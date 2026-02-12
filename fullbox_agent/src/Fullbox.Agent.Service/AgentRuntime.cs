using System.Net.Http.Json;
using System.Reflection;
using System.Text.Json;
using System.Drawing.Printing;
using System.IO.Ports;
using System.Management;
using System.Text.RegularExpressions;
using Fullbox.Agent.Shared;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Service;

public sealed class AgentRuntime
{
    private readonly ILogger<AgentRuntime> _logger;
    private readonly HttpClient _client = new();
    private readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };
    private Dictionary<string, object> _comStatus = new();
    private List<Dictionary<string, object>> _comDevices = new();
    private DateTime _comDevicesAt = DateTime.MinValue;

    public AgentRuntime(ILogger<AgentRuntime> logger)
    {
        _logger = logger;
        Config = ConfigStore.LoadOrCreate();
        ApplyHeaders();
    }

    public AgentConfig Config { get; private set; }

    public void UpdateComStatus(Dictionary<string, object>? status)
    {
        _comStatus = status ?? new Dictionary<string, object>();
    }

    public Dictionary<string, object> GetComStatusSnapshot()
    {
        return SnapshotComStatus();
    }

    public IReadOnlyList<Dictionary<string, object>> GetComDevicesSnapshot()
    {
        return SnapshotComDevices();
    }

    private Dictionary<string, object> SnapshotComStatus()
    {
        return _comStatus ?? new Dictionary<string, object>();
    }

    private List<Dictionary<string, object>> SnapshotComDevices()
    {
        if ((DateTime.UtcNow - _comDevicesAt).TotalSeconds < 10 && _comDevices.Count > 0)
        {
            return _comDevices;
        }
        _comDevices = LoadComDevices();
        _comDevicesAt = DateTime.UtcNow;
        return _comDevices;
    }

    public void ReloadConfig()
    {
        Config = ConfigStore.LoadOrCreate();
    }

    private void ApplyHeaders()
    {
        _client.BaseAddress = new Uri(Config.BaseUrl.TrimEnd('/') + "/");
        _client.DefaultRequestHeaders.Remove("X-Agent-Token");
        if (!string.IsNullOrWhiteSpace(Config.Token))
        {
            _client.DefaultRequestHeaders.Add("X-Agent-Token", Config.Token);
        }
    }

    public async Task<bool> PingAsync(CancellationToken token)
    {
        var printers = new List<string>();
        try
        {
            foreach (var printer in PrinterSettings.InstalledPrinters)
            {
                var value = printer?.ToString();
                if (!string.IsNullOrWhiteSpace(value))
                {
                    printers.Add(value);
                }
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to read installed printers");
        }

        var comPorts = Array.Empty<string>();
        try
        {
            comPorts = SerialPort.GetPortNames();
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to read COM ports");
            comPorts = Array.Empty<string>();
        }

        var comStatus = SnapshotComStatus();
        var comHealth = BuildComHealth(comPorts, comStatus);

        var payload = new AgentPing
        {
            AgentId = Config.AgentId,
            Name = Config.Name,
            Host = Config.Host,
            Version = GetVersion(),
            Meta = new Dictionary<string, object>
            {
                ["printers"] = printers,
                ["com"] = new Dictionary<string, object>
                {
                    ["enabled"] = Config.Com.Enabled,
                    ["port"] = Config.Com.PortName,
                    ["baud"] = Config.Com.BaudRate,
                    ["eol"] = Config.Com.Eol.ToString(),
                    ["idle_ms"] = Config.Com.IdleMs,
                },
                ["com_ports"] = comPorts,
                ["com_status"] = comStatus,
                ["com_devices"] = SnapshotComDevices(),
                ["com_health"] = comHealth,
            },
        };

        try
        {
            var response = await _client.PostAsJsonAsync("agent/ping/", payload, _jsonOptions, token);
            return response.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Ping failed");
            return false;
        }
    }

    public async Task<IReadOnlyList<AgentCommand>> FetchCommandsAsync(CancellationToken token)
    {
        try
        {
            var url = $"agent/commands/?agent_id={Uri.EscapeDataString(Config.AgentId)}";
            var response = await _client.GetAsync(url, token);
            if (!response.IsSuccessStatusCode)
            {
                return Array.Empty<AgentCommand>();
            }
            var data = await response.Content.ReadFromJsonAsync<CommandsResponse>(_jsonOptions, token);
            return data?.Commands ?? new List<AgentCommand>();
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Fetch commands failed");
            return Array.Empty<AgentCommand>();
        }
    }

    public async Task AckAsync(long commandId, bool ok, Dictionary<string, object>? result, string? error, CancellationToken token)
    {
        var payload = new AgentCommandAck
        {
            AgentId = Config.AgentId,
            Ok = ok,
            Result = result ?? new Dictionary<string, object>(),
            Error = error ?? "",
        };
        try
        {
            await _client.PostAsJsonAsync($"agent/commands/{commandId}/ack/", payload, _jsonOptions, token);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Ack failed");
        }
    }

    public async Task SendEventAsync(string eventType, Dictionary<string, object> payload, CancellationToken token)
    {
        var data = new AgentEvent
        {
            AgentId = Config.AgentId,
            EventType = eventType,
            Payload = payload,
        };
        try
        {
            await _client.PostAsJsonAsync("agent/events/", data, _jsonOptions, token);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Send event failed");
        }
    }

    private static string GetVersion()
    {
        var version = Assembly.GetExecutingAssembly().GetName().Version;
        return version?.ToString() ?? "0.0.0";
    }

    private List<Dictionary<string, object>> LoadComDevices()
    {
        var result = new List<Dictionary<string, object>>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT DeviceID, Name, PNPDeviceID, Status, StatusInfo, ProviderType, Caption, Description, ConfigManagerErrorCode, Manufacturer, Service FROM Win32_SerialPort"
            );
            foreach (ManagementObject obj in searcher.Get())
            {
                var port = obj["DeviceID"]?.ToString() ?? "";
                if (string.IsNullOrWhiteSpace(port))
                {
                    continue;
                }
                seen.Add(port);
                result.Add(SerializeDevice(port, obj, "serial"));
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "WMI serial port query failed");
        }

        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT Name, DeviceID, PNPDeviceID, Status, ConfigManagerErrorCode, Manufacturer, Service FROM Win32_PnPEntity WHERE Name LIKE '%(COM%'"
            );
            foreach (ManagementObject obj in searcher.Get())
            {
                var name = obj["Name"]?.ToString() ?? "";
                var port = ExtractPort(name);
                if (string.IsNullOrWhiteSpace(port) || seen.Contains(port))
                {
                    continue;
                }
                seen.Add(port);
                result.Add(SerializeDevice(port, obj, "pnp"));
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "WMI PnP COM query failed");
        }

        return result;
    }

    private static Dictionary<string, object> SerializeDevice(string port, ManagementBaseObject obj, string source)
    {
        var payload = new Dictionary<string, object>
        {
            ["port"] = port,
            ["source"] = source,
        };
        AddValue(payload, "name", obj["Name"]);
        AddValue(payload, "caption", obj["Caption"]);
        AddValue(payload, "description", obj["Description"]);
        AddValue(payload, "status", obj["Status"]);
        AddValue(payload, "status_info", obj["StatusInfo"]);
        AddValue(payload, "error_code", obj["ConfigManagerErrorCode"]);
        AddValue(payload, "manufacturer", obj["Manufacturer"]);
        AddValue(payload, "service", obj["Service"]);
        AddValue(payload, "pnp_id", obj["PNPDeviceID"]);
        AddValue(payload, "device_id", obj["DeviceID"]);
        return payload;
    }

    private static void AddValue(Dictionary<string, object> payload, string key, object? value)
    {
        if (value == null)
        {
            return;
        }
        var text = value.ToString();
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }
        payload[key] = text;
    }

    private static string ExtractPort(string name)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            return "";
        }
        var match = Regex.Match(name, @"\((COM\d+)\)", RegexOptions.IgnoreCase);
        return match.Success ? match.Groups[1].Value.ToUpperInvariant() : "";
    }

    private sealed class CommandsResponse
    {
        public bool Ok { get; set; }
        public List<AgentCommand> Commands { get; set; } = new();
    }

    private Dictionary<string, object> BuildComHealth(string[] comPorts, Dictionary<string, object> comStatus)
    {
        var status = new Dictionary<string, object>
        {
            ["enabled"] = Config.Com.Enabled,
            ["port"] = Config.Com.PortName ?? "",
        };
        var port = (Config.Com.PortName ?? "").Trim();
        var enabled = Config.Com.Enabled;
        var portPresent = comPorts.Any(p => p.Equals(port, StringComparison.OrdinalIgnoreCase));
        var connected = ReadBool(comStatus, "connected");
        var error = ReadString(comStatus, "error");
        var errorType = ReadString(comStatus, "error_type");
        var errorCode = ReadNumber(comStatus, "error_code");

        status["port_present"] = portPresent;
        status["connected"] = connected;
        if (!string.IsNullOrWhiteSpace(error))
        {
            status["error"] = error;
        }
        if (!string.IsNullOrWhiteSpace(errorType))
        {
            status["error_type"] = errorType;
        }
        if (errorCode.HasValue)
        {
            status["error_code"] = errorCode.Value;
        }

        if (!enabled)
        {
            status["ready"] = false;
            status["reason"] = "disabled";
            return status;
        }

        if (string.IsNullOrWhiteSpace(port))
        {
            status["ready"] = false;
            status["reason"] = "port_missing";
            return status;
        }

        if (!portPresent)
        {
            status["ready"] = false;
            status["reason"] = "port_not_found";
            return status;
        }

        if (connected)
        {
            status["ready"] = true;
            status["reason"] = "connected";
            return status;
        }

        if (!string.IsNullOrWhiteSpace(error))
        {
            status["ready"] = false;
            status["reason"] = "error";
            return status;
        }

        status["ready"] = false;
        status["reason"] = "not_connected";
        return status;
    }

    private static bool ReadBool(Dictionary<string, object> status, string key)
    {
        if (!status.TryGetValue(key, out var value) || value == null)
        {
            return false;
        }
        if (value is bool boolean)
        {
            return boolean;
        }
        if (bool.TryParse(value.ToString(), out var parsed))
        {
            return parsed;
        }
        return false;
    }

    private static string ReadString(Dictionary<string, object> status, string key)
    {
        if (!status.TryGetValue(key, out var value) || value == null)
        {
            return "";
        }
        return value.ToString() ?? "";
    }

    private static int? ReadNumber(Dictionary<string, object> status, string key)
    {
        if (!status.TryGetValue(key, out var value) || value == null)
        {
            return null;
        }
        if (value is int intValue)
        {
            return intValue;
        }
        if (value is long longValue)
        {
            return (int)longValue;
        }
        if (int.TryParse(value.ToString(), out var parsed))
        {
            return parsed;
        }
        return null;
    }
}
