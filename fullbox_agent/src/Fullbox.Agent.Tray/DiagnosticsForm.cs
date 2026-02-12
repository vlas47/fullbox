using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Windows.Forms;
using Fullbox.Agent.Service;
using Fullbox.Agent.Shared;

namespace Fullbox.Agent.Tray;

public sealed class DiagnosticsForm : Form
{
    private readonly Label _title;
    private readonly Label _status;
    private readonly Label _processes;
    private readonly Label _scannerTitle;
    private readonly TextBox _scannerLog;
    private readonly TextBox _details;
    private readonly Button _copyButton;
    private readonly Button _refreshButton;
    private readonly Button _applyButton;
    private readonly CheckBox _comEnabled;
    private readonly ComboBox _portSelect;
    private readonly ComboBox _baudSelect;
    private readonly ComboBox _eolSelect;
    private readonly NumericUpDown _idleSelect;
    private bool _dirty;
    private string _lastScanToken = "";
    private readonly List<string> _scanHistory = new();

    public DiagnosticsForm()
    {
        Text = "Диагностика Fullbox Agent";
        Width = 860;
        Height = 720;
        StartPosition = FormStartPosition.CenterScreen;

        _title = new Label
        {
            Dock = DockStyle.Top,
            Height = 28,
            Text = "Состояние агента",
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
            Padding = new Padding(12, 0, 12, 0),
            Font = new System.Drawing.Font("Segoe UI", 10, System.Drawing.FontStyle.Bold),
        };

        _status = new Label
        {
            Dock = DockStyle.Top,
            Height = 26,
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
            Padding = new Padding(12, 0, 12, 0),
        };

        _processes = new Label
        {
            Dock = DockStyle.Top,
            Height = 22,
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
            Padding = new Padding(12, 0, 12, 0),
        };

        _scannerTitle = new Label
        {
            Dock = DockStyle.Top,
            Height = 22,
            TextAlign = System.Drawing.ContentAlignment.MiddleLeft,
            Padding = new Padding(8, 2, 8, 2),
            Text = "Сканер (последние значения)",
        };

        _scannerLog = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Vertical,
            Font = new System.Drawing.Font("Consolas", 9),
            Text = "(пока нет данных)",
        };

        var settingsPanel = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            Height = 120,
            Padding = new Padding(12, 6, 12, 6),
            ColumnCount = 6,
            RowCount = 3,
            AutoSize = true,
        };
        settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120));
        settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 35));
        settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120));
        settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 35));
        settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120));
        settingsPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 30));
        settingsPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        settingsPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        settingsPanel.RowStyles.Add(new RowStyle(SizeType.Absolute, 32));

        _comEnabled = new CheckBox
        {
            Text = "Включен",
            Dock = DockStyle.Fill,
            AutoSize = true,
        };
        _comEnabled.CheckedChanged += (_, _) => MarkDirty();

        _portSelect = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDownList,
        };
        _portSelect.SelectedIndexChanged += (_, _) => MarkDirty();

        _baudSelect = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDown,
        };
        _baudSelect.Items.AddRange(new object[] { "9600", "19200", "38400", "57600", "115200" });
        _baudSelect.TextChanged += (_, _) => MarkDirty();

        _eolSelect = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDownList,
        };
        _eolSelect.Items.AddRange(new object[] { ComEol.CrLf.ToString(), ComEol.Cr.ToString(), ComEol.Lf.ToString(), ComEol.Tab.ToString() });
        _eolSelect.SelectedIndexChanged += (_, _) => MarkDirty();

        _idleSelect = new NumericUpDown
        {
            Dock = DockStyle.Fill,
            Minimum = 0,
            Maximum = 10000,
            Increment = 50,
        };
        _idleSelect.ValueChanged += (_, _) => MarkDirty();

        _refreshButton = new Button
        {
            Text = "Обновить",
            Dock = DockStyle.Fill,
        };
        _refreshButton.Click += (_, _) => RefreshRequested?.Invoke();

        _applyButton = new Button
        {
            Text = "Применить",
            Dock = DockStyle.Fill,
            Enabled = false,
        };
        _applyButton.Click += (_, _) => ApplyConfig();

        settingsPanel.Controls.Add(new Label { Text = "COM:", TextAlign = System.Drawing.ContentAlignment.MiddleLeft, Dock = DockStyle.Fill }, 0, 0);
        settingsPanel.Controls.Add(_comEnabled, 1, 0);
        settingsPanel.Controls.Add(new Label { Text = "Порт:", TextAlign = System.Drawing.ContentAlignment.MiddleLeft, Dock = DockStyle.Fill }, 2, 0);
        settingsPanel.Controls.Add(_portSelect, 3, 0);
        settingsPanel.Controls.Add(new Label { Text = "Скорость:", TextAlign = System.Drawing.ContentAlignment.MiddleLeft, Dock = DockStyle.Fill }, 4, 0);
        settingsPanel.Controls.Add(_baudSelect, 5, 0);

        settingsPanel.Controls.Add(new Label { Text = "Окончание:", TextAlign = System.Drawing.ContentAlignment.MiddleLeft, Dock = DockStyle.Fill }, 0, 1);
        settingsPanel.Controls.Add(_eolSelect, 1, 1);
        settingsPanel.Controls.Add(new Label { Text = "Таймаут (мс):", TextAlign = System.Drawing.ContentAlignment.MiddleLeft, Dock = DockStyle.Fill }, 2, 1);
        settingsPanel.Controls.Add(_idleSelect, 3, 1);

        settingsPanel.Controls.Add(_refreshButton, 4, 2);
        settingsPanel.Controls.Add(_applyButton, 5, 2);

        _details = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Vertical,
            Font = new System.Drawing.Font("Consolas", 9),
        };

        _copyButton = new Button
        {
            Dock = DockStyle.Bottom,
            Height = 32,
            Text = "Копировать в буфер",
        };
        _copyButton.Click += (_, _) =>
        {
            if (!string.IsNullOrWhiteSpace(_details.Text))
            {
                try
                {
                    Clipboard.SetText(_details.Text);
                }
                catch (Exception ex)
                {
                    MessageBox.Show(
                        this,
                        $"Не удалось скопировать: {ex.Message}",
                        "Fullbox Agent",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                }
            }
        };

        var scannerPanel = new Panel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(6),
        };
        scannerPanel.Controls.Add(_scannerLog);
        scannerPanel.Controls.Add(_scannerTitle);

        var split = new SplitContainer
        {
            Dock = DockStyle.Fill,
            Orientation = Orientation.Horizontal,
            SplitterDistance = 360,
            Panel1MinSize = 200,
            Panel2MinSize = 120,
        };
        split.Panel1.Controls.Add(_details);
        split.Panel2.Controls.Add(scannerPanel);

        Controls.Add(split);
        Controls.Add(_copyButton);
        Controls.Add(settingsPanel);
        Controls.Add(_processes);
        Controls.Add(_status);
        Controls.Add(_title);
    }

    public event Action? RefreshRequested;
    public event Action<DiagnosticsConfigChange>? ApplyRequested;

    public void UpdateSnapshot(DiagnosticsSnapshot snapshot)
    {
        _status.Text = snapshot.Hardware.Ready
            ? $"Готово к работе · {snapshot.Hardware.Reason}"
            : $"Ошибка подключения · {snapshot.Hardware.Reason}";
        _processes.Text = $"Процессы: Tray — {DescribeProcess("Fullbox.Agent.Tray")}; Service — {DescribeProcess("Fullbox.Agent.Service")}";

        UpdateInputs(snapshot);
        UpdateScannerLog(snapshot);

        var sb = new StringBuilder();
        sb.AppendLine($"Время: {snapshot.Now:dd.MM.yyyy HH:mm:ss}");
        sb.AppendLine($"Agent ID: {snapshot.Config.AgentId}");
        sb.AppendLine($"Имя: {snapshot.Config.Name}");
        sb.AppendLine($"Host: {snapshot.Config.Host}");
        sb.AppendLine($"Версия: {snapshot.Version}");
        sb.AppendLine($"Base URL: {snapshot.Config.BaseUrl}");
        sb.AppendLine($"Ping interval: {snapshot.Config.PingIntervalSec} c");
        sb.AppendLine($"Poll interval: {snapshot.Config.PollIntervalSec} c");
        sb.AppendLine();

        sb.AppendLine("Процессы агента:");
        AppendProcessInfo(sb, "Tray", "Fullbox.Agent.Tray");
        AppendProcessInfo(sb, "Service", "Fullbox.Agent.Service");
        sb.AppendLine();

        sb.AppendLine("COM настройки:");
        sb.AppendLine($"  enabled: {snapshot.Config.Com.Enabled}");
        sb.AppendLine($"  port: {snapshot.Config.Com.PortName}");
        sb.AppendLine($"  baud: {snapshot.Config.Com.BaudRate}");
        sb.AppendLine($"  eol: {snapshot.Config.Com.Eol}");
        sb.AppendLine($"  idle_ms: {snapshot.Config.Com.IdleMs}");
        sb.AppendLine();

        sb.AppendLine("COM статус:");
        AppendDictionary(sb, snapshot.ComStatus, "  ");
        sb.AppendLine();

        sb.AppendLine("COM порты:");
        if (snapshot.ComPorts.Count == 0)
        {
            sb.AppendLine("  (не найдены)");
        }
        else
        {
            foreach (var port in snapshot.ComPorts)
            {
                sb.AppendLine($"  {port}");
            }
        }
        sb.AppendLine();

        sb.AppendLine("COM устройства (WMI):");
        if (snapshot.ComDevices.Count == 0)
        {
            sb.AppendLine("  (нет данных)");
        }
        else
        {
            var index = 1;
            foreach (var device in snapshot.ComDevices)
            {
                sb.AppendLine($"  {index}. {ReadValue(device, "port")} · {ReadValue(device, "name")} · {ReadValue(device, "status")}");
                AppendDictionary(sb, device, "     ");
                index++;
            }
        }

        _details.Text = sb.ToString();
    }

    private void UpdateInputs(DiagnosticsSnapshot snapshot)
    {
        if (!_dirty)
        {
            _comEnabled.Checked = snapshot.Config.Com.Enabled;
            _baudSelect.Text = snapshot.Config.Com.BaudRate.ToString();
            _idleSelect.Value = Math.Min(_idleSelect.Maximum, Math.Max(_idleSelect.Minimum, snapshot.Config.Com.IdleMs));

            var eol = snapshot.Config.Com.Eol.ToString();
            if (_eolSelect.Items.Contains(eol))
            {
                _eolSelect.SelectedItem = eol;
            }
            else
            {
                _eolSelect.SelectedIndex = 0;
            }
        }

        var current = snapshot.Config.Com.PortName ?? "";
        var ports = snapshot.ComPorts?.ToList() ?? new List<string>();
        if (!string.IsNullOrWhiteSpace(current) && !ports.Contains(current, StringComparer.OrdinalIgnoreCase))
        {
            ports.Insert(0, current);
        }
        if (ports.Count == 0)
        {
            ports.Add("-");
        }

        if (!_portSelect.Focused && !_portSelect.DroppedDown)
        {
            _portSelect.BeginUpdate();
            _portSelect.Items.Clear();
            _portSelect.Items.AddRange(ports.Cast<object>().ToArray());
            _portSelect.SelectedItem = ports.FirstOrDefault(p => p.Equals(current, StringComparison.OrdinalIgnoreCase)) ?? ports[0];
            _portSelect.EndUpdate();
        }
    }

    private void ApplyConfig()
    {
        var port = _portSelect.SelectedItem?.ToString() ?? "";
        if (port == "-")
        {
            port = "";
        }
        var baud = 9600;
        if (!int.TryParse(_baudSelect.Text.Trim(), out baud))
        {
            baud = 9600;
        }
        var eolText = _eolSelect.SelectedItem?.ToString() ?? ComEol.CrLf.ToString();
        if (!Enum.TryParse<ComEol>(eolText, true, out var eol))
        {
            eol = ComEol.CrLf;
        }

        var change = new DiagnosticsConfigChange(
            _comEnabled.Checked,
            port,
            baud,
            eol,
            (int)_idleSelect.Value
        );
        ApplyRequested?.Invoke(change);
        _dirty = false;
        _applyButton.Enabled = false;
    }

    private void MarkDirty()
    {
        _dirty = true;
        _applyButton.Enabled = true;
    }

    private static void AppendDictionary(StringBuilder sb, Dictionary<string, object> data, string indent)
    {
        foreach (var item in data.OrderBy(kvp => kvp.Key, StringComparer.OrdinalIgnoreCase))
        {
            var value = item.Value?.ToString() ?? "";
            sb.AppendLine($"{indent}{item.Key}: {value}");
        }
    }

    private static void AppendProcessInfo(StringBuilder sb, string label, string processName)
    {
        sb.AppendLine($"  {label}: {DescribeProcess(processName)}");
    }

    private static string DescribeProcess(string processName)
    {
        try
        {
            var processes = Process.GetProcessesByName(processName);
            if (processes.Length == 0)
            {
                return "не запущен";
            }

            var pids = string.Join(", ", processes.Select(proc => proc.Id).OrderBy(id => id));
            return $"запущен (PID {pids})";
        }
        catch (Exception ex)
        {
            return $"ошибка проверки: {ex.Message}";
        }
    }

    private static string ReadValue(Dictionary<string, object> data, string key)
    {
        return data.TryGetValue(key, out var value) ? value?.ToString() ?? "" : "";
    }

    private void UpdateScannerLog(DiagnosticsSnapshot snapshot)
    {
        var status = snapshot.ComStatus;
        var lastAt = ReadValue(status, "last_scan_at");
        var lastValue = ReadValue(status, "last_scan_value");
        if (string.IsNullOrWhiteSpace(lastAt) || string.IsNullOrWhiteSpace(lastValue))
        {
            if (_scanHistory.Count == 0)
            {
                _scannerLog.Text = "(пока нет данных)";
            }
            return;
        }

        var token = $"{lastAt}|{lastValue}";
        if (string.Equals(token, _lastScanToken, StringComparison.Ordinal))
        {
            return;
        }

        _lastScanToken = token;
        var stamp = FormatScanTime(lastAt);
        _scanHistory.Add($"{stamp} · {lastValue}");
        if (_scanHistory.Count > 200)
        {
            _scanHistory.RemoveAt(0);
        }
        _scannerLog.Text = string.Join(Environment.NewLine, _scanHistory);
        _scannerLog.SelectionStart = _scannerLog.TextLength;
        _scannerLog.ScrollToCaret();
    }

    private static string FormatScanTime(string iso)
    {
        if (DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto))
        {
            return dto.ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss");
        }
        return iso;
    }
}

public sealed record DiagnosticsSnapshot(
    DateTime Now,
    HardwareState Hardware,
    AgentConfig Config,
    string Version,
    Dictionary<string, object> ComStatus,
    IReadOnlyList<string> ComPorts,
    IReadOnlyList<Dictionary<string, object>> ComDevices
);

public sealed record HardwareState(bool Ready, string Reason, string Details);

public sealed record DiagnosticsConfigChange(
    bool Enabled,
    string Port,
    int Baud,
    ComEol Eol,
    int IdleMs
);
