using Fullbox.Agent.Shared;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Service;

public sealed class Worker : BackgroundService
{
    private readonly ILogger<Worker> _logger;
    private readonly AgentRuntime _runtime;
    private readonly ComScanner _scanner;
    private readonly PrinterController _printerController;
    private readonly PrintJobRunner _printRunner;

    public Worker(
        ILogger<Worker> logger,
        AgentRuntime runtime,
        ComScanner scanner,
        PrinterController printerController,
        PrintJobRunner printRunner
    )
    {
        _logger = logger;
        _runtime = runtime;
        _scanner = scanner;
        _printerController = printerController;
        _printRunner = printRunner;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _logger.LogInformation("Fullbox agent started");
        var pingTask = RunPingLoop(stoppingToken);
        var cmdTask = RunCommandLoop(stoppingToken);
        var scanTask = _scanner.RunAsync(stoppingToken);
        var printTask = _printRunner.RunAsync(stoppingToken);
        await Task.WhenAll(pingTask, cmdTask, scanTask, printTask);
    }

    private async Task RunPingLoop(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            _runtime.ReloadConfig();
            await _runtime.PingAsync(token);
            await Task.Delay(TimeSpan.FromSeconds(_runtime.Config.PingIntervalSec), token);
        }
    }

    private async Task RunCommandLoop(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            var commands = await _runtime.FetchCommandsAsync(token);
            foreach (var command in commands)
            {
                await HandleCommandAsync(command, token);
            }
            await Task.Delay(TimeSpan.FromSeconds(_runtime.Config.PollIntervalSec), token);
        }
    }

    private async Task HandleCommandAsync(AgentCommand command, CancellationToken token)
    {
        try
        {
            if (string.Equals(command.Command, "agent.reload_config", StringComparison.OrdinalIgnoreCase))
            {
                _runtime.ReloadConfig();
                await _runtime.AckAsync(command.Id, true, new Dictionary<string, object> { ["status"] = "reloaded" }, "", token);
                return;
            }

            if (string.Equals(command.Command, "printer.list", StringComparison.OrdinalIgnoreCase))
            {
                var printers = _printerController.ListPrinters();
                await _runtime.AckAsync(
                    command.Id,
                    true,
                    new Dictionary<string, object> { ["printers"] = printers },
                    "",
                    token
                );
                return;
            }

            if (string.Equals(command.Command, "scanner.list_ports", StringComparison.OrdinalIgnoreCase))
            {
                var ports = _scanner.ListPorts();
                await _runtime.AckAsync(
                    command.Id,
                    true,
                    new Dictionary<string, object> { ["ports"] = ports },
                    "",
                    token
                );
                return;
            }

            if (string.Equals(command.Command, "scanner.status", StringComparison.OrdinalIgnoreCase))
            {
                var status = _scanner.GetStatus();
                status["ports"] = _scanner.ListPorts();
                await _runtime.AckAsync(command.Id, true, status, "", token);
                return;
            }

            if (string.Equals(command.Command, "scanner.test", StringComparison.OrdinalIgnoreCase))
            {
                var config = _runtime.Config;
                var port = ReadPayloadString(command.Payload, "port");
                if (string.IsNullOrWhiteSpace(port))
                {
                    port = config.Com.PortName;
                }
                var baud = ReadPayloadInt(command.Payload, "baud") ?? config.Com.BaudRate;
                var eol = ReadPayloadEol(command.Payload, "eol") ?? config.Com.Eol;
                var result = _scanner.TestOpen(port, baud, eol);
                await _runtime.AckAsync(command.Id, true, result, "", token);
                return;
            }

            if (string.Equals(command.Command, "scanner.reconnect", StringComparison.OrdinalIgnoreCase))
            {
                _scanner.RequestReconnect();
                await _runtime.AckAsync(command.Id, true, new Dictionary<string, object> { ["reconnect"] = true }, "", token);
                return;
            }

            if (string.Equals(command.Command, "scanner.config", StringComparison.OrdinalIgnoreCase))
            {
                var config = _runtime.Config;
                var updated = false;
                var disabled = false;
                var reconnectRequested = false;
                var enabled = ReadPayloadBool(command.Payload, "enabled");
                if (enabled.HasValue)
                {
                    config.Com.Enabled = enabled.Value;
                    updated = true;
                    if (!enabled.Value)
                    {
                        disabled = true;
                    }
                    else
                    {
                        reconnectRequested = true;
                    }
                }
                var port = ReadPayloadString(command.Payload, "port");
                if (!string.IsNullOrWhiteSpace(port))
                {
                    config.Com.PortName = port;
                    updated = true;
                    reconnectRequested = true;
                }
                var baud = ReadPayloadInt(command.Payload, "baud");
                if (baud.HasValue && baud.Value > 0)
                {
                    config.Com.BaudRate = baud.Value;
                    updated = true;
                    reconnectRequested = true;
                }
                var eol = ReadPayloadEol(command.Payload, "eol");
                if (eol.HasValue)
                {
                    config.Com.Eol = eol.Value;
                    updated = true;
                    reconnectRequested = true;
                }
                var idle = ReadPayloadInt(command.Payload, "idle_ms");
                if (idle.HasValue && idle.Value >= 0)
                {
                    config.Com.IdleMs = idle.Value;
                    updated = true;
                }
                if (updated)
                {
                    ConfigStore.Save(config);
                    _runtime.ReloadConfig();
                }
                var reconnect = ReadPayloadBool(command.Payload, "reconnect");
                if (disabled)
                {
                    _scanner.Disable();
                }
                else if ((reconnect.HasValue && reconnect.Value) || reconnectRequested)
                {
                    _scanner.RequestReconnect();
                }
                var result = _scanner.GetStatus();
                await _runtime.AckAsync(command.Id, true, result, "", token);
                return;
            }

            if (string.Equals(command.Command, "printer.pause", StringComparison.OrdinalIgnoreCase)
                || string.Equals(command.Command, "printer.resume", StringComparison.OrdinalIgnoreCase)
                || string.Equals(command.Command, "printer.clear", StringComparison.OrdinalIgnoreCase)
                || string.Equals(command.Command, "printer.status", StringComparison.OrdinalIgnoreCase))
            {
                var printerName = ReadPayloadString(command.Payload, "printer");
                if (string.IsNullOrWhiteSpace(printerName))
                {
                    await _runtime.AckAsync(command.Id, false, null, "printer_required", token);
                    return;
                }
                if (string.Equals(command.Command, "printer.pause", StringComparison.OrdinalIgnoreCase))
                {
                    var ok = _printerController.Pause(printerName);
                    await _runtime.AckAsync(command.Id, ok, new Dictionary<string, object> { ["paused"] = ok }, ok ? "" : "not_found", token);
                    return;
                }
                if (string.Equals(command.Command, "printer.resume", StringComparison.OrdinalIgnoreCase))
                {
                    var ok = _printerController.Resume(printerName);
                    await _runtime.AckAsync(command.Id, ok, new Dictionary<string, object> { ["resumed"] = ok }, ok ? "" : "not_found", token);
                    return;
                }
                if (string.Equals(command.Command, "printer.clear", StringComparison.OrdinalIgnoreCase))
                {
                    var ok = _printerController.Clear(printerName);
                    await _runtime.AckAsync(command.Id, ok, new Dictionary<string, object> { ["cleared"] = ok }, ok ? "" : "not_found", token);
                    return;
                }
                if (string.Equals(command.Command, "printer.status", StringComparison.OrdinalIgnoreCase))
                {
                    var status = _printerController.Status(printerName);
                    await _runtime.AckAsync(command.Id, true, status, "", token);
                    return;
                }
            }

            await _runtime.AckAsync(command.Id, false, null, "not_implemented", token);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Command failed {Command}", command.Command);
            await _runtime.AckAsync(command.Id, false, null, ex.Message, token);
        }
    }

    private static string ReadPayloadString(Dictionary<string, object> payload, string key)
    {
        if (payload == null || !payload.TryGetValue(key, out var value) || value == null)
        {
            return "";
        }
        if (value is System.Text.Json.JsonElement element)
        {
            if (element.ValueKind == System.Text.Json.JsonValueKind.String)
            {
                return element.GetString() ?? "";
            }
            return element.ToString();
        }
        return value.ToString() ?? "";
    }

    private static bool? ReadPayloadBool(Dictionary<string, object> payload, string key)
    {
        if (payload == null || !payload.TryGetValue(key, out var value) || value == null)
        {
            return null;
        }
        if (value is bool boolean)
        {
            return boolean;
        }
        if (value is System.Text.Json.JsonElement element)
        {
            if (element.ValueKind == System.Text.Json.JsonValueKind.True)
            {
                return true;
            }
            if (element.ValueKind == System.Text.Json.JsonValueKind.False)
            {
                return false;
            }
            if (element.ValueKind == System.Text.Json.JsonValueKind.String)
            {
                value = element.GetString();
            }
            else
            {
                value = element.ToString();
            }
        }
        var text = value?.ToString()?.Trim().ToLowerInvariant();
        return text switch
        {
            "1" => true,
            "true" => true,
            "yes" => true,
            "on" => true,
            "0" => false,
            "false" => false,
            "no" => false,
            "off" => false,
            _ => null,
        };
    }

    private static int? ReadPayloadInt(Dictionary<string, object> payload, string key)
    {
        if (payload == null || !payload.TryGetValue(key, out var value) || value == null)
        {
            return null;
        }
        if (value is int number)
        {
            return number;
        }
        if (value is long longNumber)
        {
            return (int)longNumber;
        }
        if (value is System.Text.Json.JsonElement element)
        {
            if (element.ValueKind == System.Text.Json.JsonValueKind.Number && element.TryGetInt32(out var parsed))
            {
                return parsed;
            }
            if (element.ValueKind == System.Text.Json.JsonValueKind.String)
            {
                value = element.GetString();
            }
            else
            {
                value = element.ToString();
            }
        }
        if (int.TryParse(value?.ToString(), out var result))
        {
            return result;
        }
        return null;
    }

    private static ComEol? ReadPayloadEol(Dictionary<string, object> payload, string key)
    {
        var text = ReadPayloadString(payload, key);
        if (string.IsNullOrWhiteSpace(text))
        {
            return null;
        }
        if (Enum.TryParse<ComEol>(text, true, out var result))
        {
            return result;
        }
        return null;
    }
}
