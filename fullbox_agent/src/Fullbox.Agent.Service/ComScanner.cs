using System.IO.Ports;
using System.Linq;
using Fullbox.Agent.Shared;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Service;

public sealed class ComScanner
{
    private readonly AgentRuntime _runtime;
    private readonly ILogger<ComScanner> _logger;
    private SerialPort? _port;
    private string _buffer = "";
    private DateTime _lastChunk = DateTime.MinValue;
    private DateTime _lastScan = DateTime.MinValue;
    private DateTime _lastStatusReport = DateTime.MinValue;
    private string _lastError = "";
    private DateTime _lastErrorAt = DateTime.MinValue;
    private string _lastErrorType = "";
    private int _lastErrorCode;
    private bool _lastConnected;
    private string _lastScanValue = "";
    private ComSnapshot? _activeSettings;
    private bool _reconnectRequested;

    public ComScanner(AgentRuntime runtime, ILogger<ComScanner> logger)
    {
        _runtime = runtime;
        _logger = logger;
    }

    public async Task RunAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            var config = _runtime.Config;
            var snapshot = ComSnapshot.From(config.Com);
            if (_reconnectRequested || _activeSettings == null || !_activeSettings.Equals(snapshot))
            {
                _reconnectRequested = false;
                _activeSettings = snapshot;
                ClosePort();
            }

            if (!snapshot.Enabled)
            {
                ClosePort();
                await Task.Delay(TimeSpan.FromSeconds(2), token);
                continue;
            }

            try
            {
                EnsurePort(snapshot);
                if (_port == null || !_port.IsOpen)
                {
                    ReportStatus();
                    await Task.Delay(TimeSpan.FromSeconds(1), token);
                    continue;
                }

                var chunk = _port.ReadExisting();
                if (!string.IsNullOrEmpty(chunk))
                {
                    AppendChunk(chunk);
                }
                else
                {
                    FlushOnIdle(snapshot);
                }
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "COM read error");
                _lastError = ex.Message;
                _lastErrorAt = DateTime.UtcNow;
                _lastErrorType = ex.GetType().Name;
                _lastErrorCode = ex.HResult;
                ClosePort();
                ReportStatus(true);
                await Task.Delay(TimeSpan.FromSeconds(2), token);
            }

            ReportStatus();
            await Task.Delay(25, token);
        }
    }

    public void RequestReconnect()
    {
        _reconnectRequested = true;
    }

    public void Disable()
    {
        _reconnectRequested = false;
        _activeSettings = null;
        _lastError = "";
        _lastErrorAt = DateTime.MinValue;
        _lastErrorType = "";
        _lastErrorCode = 0;
        _lastScan = DateTime.MinValue;
        _lastChunk = DateTime.MinValue;
        _lastScanValue = "";
        _buffer = "";
        ClosePort();
        ReportStatus(true);
    }

    public IReadOnlyList<string> ListPorts()
    {
        try
        {
            return SerialPort.GetPortNames().OrderBy(name => name).ToArray();
        }
        catch
        {
            return Array.Empty<string>();
        }
    }

    public Dictionary<string, object> TestOpen(string portName, int baudRate, ComEol eol)
    {
        var result = new Dictionary<string, object>
        {
            ["port"] = portName,
            ["baud"] = baudRate,
            ["eol"] = eol.ToString(),
        };
        if (string.IsNullOrWhiteSpace(portName))
        {
            result["ok"] = false;
            result["error"] = "port_required";
            return result;
        }

        if (_port != null && _port.IsOpen && string.Equals(_port.PortName, portName, StringComparison.OrdinalIgnoreCase))
        {
            result["ok"] = true;
            result["connected"] = true;
            result["note"] = "already_open_by_agent";
            return result;
        }

        try
        {
            using var testPort = new SerialPort(portName, baudRate, Parity.None, 8, StopBits.One)
            {
                NewLine = ResolveDelimiter(eol),
                ReadTimeout = 250,
            };
            testPort.DtrEnable = true;
            testPort.RtsEnable = true;
            testPort.Open();
            result["ok"] = true;
            result["connected"] = testPort.IsOpen;
        }
        catch (Exception ex)
        {
            result["ok"] = false;
            result["error"] = ex.Message;
            result["error_type"] = ex.GetType().Name;
            result["error_code"] = ex.HResult;
        }

        return result;
    }

    public Dictionary<string, object> GetStatus()
    {
        var settings = _activeSettings ?? ComSnapshot.From(_runtime.Config.Com);
        var status = new Dictionary<string, object>
        {
            ["connected"] = _port != null && _port.IsOpen,
            ["enabled"] = settings.Enabled,
            ["port"] = settings.Port,
            ["baud"] = settings.Baud,
            ["eol"] = settings.Eol.ToString(),
            ["idle_ms"] = settings.IdleMs,
        };
        if (!string.IsNullOrWhiteSpace(_lastError))
        {
            status["error"] = _lastError;
            status["error_at"] = _lastErrorAt == DateTime.MinValue ? "" : _lastErrorAt.ToString("O");
            status["error_type"] = _lastErrorType;
            status["error_code"] = _lastErrorCode;
        }
        if (_lastScan != DateTime.MinValue)
        {
            status["last_scan_at"] = _lastScan.ToString("O");
            if (!string.IsNullOrWhiteSpace(_lastScanValue))
            {
                status["last_scan_value"] = _lastScanValue;
                status["last_scan_len"] = _lastScanValue.Length;
            }
        }
        if (_lastChunk != DateTime.MinValue)
        {
            status["last_chunk_at"] = _lastChunk.ToString("O");
        }
        return status;
    }

    private void EnsurePort(ComSnapshot snapshot)
    {
        if (_port != null && _port.IsOpen && _port.PortName == snapshot.Port)
        {
            return;
        }

        ClosePort();
        _port = new SerialPort(snapshot.Port, snapshot.Baud, Parity.None, 8, StopBits.One)
        {
            NewLine = ResolveDelimiter(snapshot.Eol),
            ReadTimeout = 250,
        };
        _port.DtrEnable = true;
        _port.RtsEnable = true;
        _port.Open();
        _lastError = "";
        _lastErrorAt = DateTime.MinValue;
        _lastErrorType = "";
        _lastErrorCode = 0;
        _logger.LogInformation("COM connected {Port} {Baud}", snapshot.Port, snapshot.Baud);
        ReportStatus(true);
    }

    private void ClosePort()
    {
        try
        {
            if (_port != null)
            {
                _port.Close();
                _port.Dispose();
            }
        }
        catch
        {
            // ignore close errors
        }
        _port = null;
        _buffer = "";
        _lastChunk = DateTime.MinValue;
        _lastConnected = false;
    }

    private void AppendChunk(string chunk)
    {
        _buffer += chunk;
        _lastChunk = DateTime.UtcNow;
        var delimiter = ResolveDelimiter(_activeSettings?.Eol ?? ComEol.CrLf);
        if (!string.IsNullOrEmpty(delimiter))
        {
            SplitByDelimiter(delimiter);
        }
    }

    private void SplitByDelimiter(string delimiter)
    {
        var index = _buffer.IndexOf(delimiter, StringComparison.Ordinal);
        while (index >= 0)
        {
            var part = _buffer[..index];
            _buffer = _buffer[(index + delimiter.Length)..];
            HandleScan(part);
            index = _buffer.IndexOf(delimiter, StringComparison.Ordinal);
        }
    }

    private void FlushOnIdle(ComSnapshot snapshot)
    {
        if (string.IsNullOrEmpty(_buffer))
        {
            return;
        }
        if (_lastChunk == DateTime.MinValue)
        {
            return;
        }
        var idleMs = (DateTime.UtcNow - _lastChunk).TotalMilliseconds;
        if (idleMs < snapshot.IdleMs)
        {
            return;
        }
        HandleScan(_buffer);
        _buffer = "";
        _lastChunk = DateTime.MinValue;
    }

    private void HandleScan(string raw)
    {
        var value = (raw ?? "").Trim();
        if (string.IsNullOrEmpty(value))
        {
            return;
        }
        _lastScan = DateTime.UtcNow;
        _lastScanValue = value;
        _logger.LogInformation("Scan received {Value}", value);
        ReportStatus(true);
        _ = _runtime.SendEventAsync(
            "scan",
            new Dictionary<string, object>
            {
                ["value"] = value,
                ["source"] = "com",
                ["port"] = _runtime.Config.Com.PortName,
            },
            CancellationToken.None
        );
    }

    private void ReportStatus(bool force = false)
    {
        var connected = _port != null && _port.IsOpen;
        var now = DateTime.UtcNow;
        if (!force && connected == _lastConnected && (now - _lastStatusReport).TotalSeconds < 1)
        {
            return;
        }
        _lastConnected = connected;
        _lastStatusReport = now;
        _runtime.UpdateComStatus(GetStatus());
    }

    private static string ResolveDelimiter(ComEol eol)
    {
        return eol switch
        {
            ComEol.CrLf => "\r\n",
            ComEol.Cr => "\r",
            ComEol.Lf => "\n",
            ComEol.Tab => "\t",
            _ => "",
        };
    }

    private sealed record ComSnapshot(bool Enabled, string Port, int Baud, ComEol Eol, int IdleMs)
    {
        public static ComSnapshot From(ComSettings settings)
        {
            return new ComSnapshot(
                settings.Enabled,
                settings.PortName ?? "",
                settings.BaudRate,
                settings.Eol,
                settings.IdleMs
            );
        }
    }
}
