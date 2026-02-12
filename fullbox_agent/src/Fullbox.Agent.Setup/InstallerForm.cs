using System;
using System.Drawing;
using System.Windows.Forms;

namespace Fullbox.Agent.Setup;

internal sealed class InstallerForm : Form
{
    private readonly ListView _steps;
    private readonly ProgressBar _progress;
    private readonly Label _status;
    private readonly Button _close;
    private bool _allowClose;

    public InstallerForm(string version)
    {
        Text = $"Установка Fullbox Agent v{version}";
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(860, 520);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 3,
            Padding = new Padding(12),
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var header = new Label
        {
            Text = $"Лента установки · v{version}",
            AutoSize = true,
            Font = new Font(Font, FontStyle.Bold),
            Margin = new Padding(0, 0, 0, 8),
        };

        _steps = new ListView
        {
            Dock = DockStyle.Fill,
            View = View.Details,
            FullRowSelect = true,
            GridLines = true,
            HideSelection = false,
        };
        _steps.Columns.Add("Время", 90);
        _steps.Columns.Add("Шаг", 220);
        _steps.Columns.Add("Детали", 420);
        _steps.Columns.Add("Статус", 110);

        var footer = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 3,
            RowCount = 2,
            Margin = new Padding(0, 8, 0, 0),
        };
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        footer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        footer.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        _status = new Label
        {
            Text = "Готов к установке.",
            AutoSize = true,
            Dock = DockStyle.Fill,
        };

        _progress = new ProgressBar
        {
            Dock = DockStyle.Fill,
            Minimum = 0,
            Maximum = 1,
            Value = 0,
            Height = 18,
        };

        _close = new Button
        {
            Text = "Закрыть",
            Enabled = false,
            AutoSize = true,
            Anchor = AnchorStyles.Right,
        };
        _close.Click += (_, _) => Close();

        footer.Controls.Add(_status, 0, 0);
        footer.Controls.Add(_close, 2, 0);
        footer.Controls.Add(_progress, 0, 1);
        footer.SetColumnSpan(_progress, 3);

        layout.Controls.Add(header, 0, 0);
        layout.Controls.Add(_steps, 0, 1);
        layout.Controls.Add(footer, 0, 2);

        Controls.Add(layout);

        SizeChanged += (_, _) => AdjustColumns();
        Shown += (_, _) => AdjustColumns();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!_allowClose)
        {
            e.Cancel = true;
            return;
        }
        base.OnFormClosing(e);
    }

    public void SetTotalSteps(int total)
    {
        SafeInvoke(() =>
        {
            _progress.Value = 0;
            _progress.Maximum = Math.Max(1, total);
        });
    }

    public ListViewItem BeginStep(string title, string details)
    {
        ListViewItem? item = null;
        SafeInvoke(() =>
        {
            var time = DateTime.Now.ToString("HH:mm:ss");
            item = new ListViewItem(new[] { time, title, details, "В работе" });
            _steps.Items.Add(item);
            item.EnsureVisible();
            _status.Text = $"Выполняется: {title}";
        });
        return item ?? new ListViewItem();
    }

    public void CompleteStep(ListViewItem item, string? details = null)
    {
        SafeInvoke(() =>
        {
            UpdateItem(item, details, "Готово");
            IncrementProgress();
            _status.Text = "Шаг выполнен.";
        });
    }

    public void WarnStep(ListViewItem item, string? details = null)
    {
        SafeInvoke(() =>
        {
            UpdateItem(item, details, "Предупреждение");
            IncrementProgress();
            _status.Text = "Шаг выполнен с предупреждением.";
        });
    }

    public void FailStep(ListViewItem item, string? details = null)
    {
        SafeInvoke(() =>
        {
            UpdateItem(item, details, "Ошибка");
            IncrementProgress();
            _status.Text = "Ошибка на шаге.";
        });
    }

    public void SetStatus(string text)
    {
        SafeInvoke(() => _status.Text = text);
    }

    public void MarkSuccess()
    {
        SafeInvoke(() =>
        {
            _progress.Value = _progress.Maximum;
            _status.Text = "Установка завершена успешно.";
            _close.Enabled = true;
            _allowClose = true;
        });
    }

    public void MarkFailure(string message)
    {
        SafeInvoke(() =>
        {
            _status.Text = message;
            _close.Enabled = true;
            _allowClose = true;
        });
    }

    public void AllowClose(string message)
    {
        SafeInvoke(() =>
        {
            _status.Text = message;
            _close.Enabled = true;
            _allowClose = true;
        });
    }

    private void UpdateItem(ListViewItem item, string? details, string status)
    {
        if (item.SubItems.Count < 4)
        {
            return;
        }
        if (!string.IsNullOrWhiteSpace(details))
        {
            item.SubItems[2].Text = details;
        }
        item.SubItems[3].Text = status;
        item.EnsureVisible();
    }

    private void IncrementProgress()
    {
        if (_progress.Value < _progress.Maximum)
        {
            _progress.Value += 1;
        }
    }

    private void SafeInvoke(Action action)
    {
        if (InvokeRequired)
        {
            Invoke(action);
            return;
        }
        action();
    }

    private void AdjustColumns()
    {
        if (_steps.Columns.Count < 4)
        {
            return;
        }
        var total = _steps.ClientSize.Width;
        var fixedWidth = _steps.Columns[0].Width + _steps.Columns[1].Width + _steps.Columns[3].Width + 4;
        var detailsWidth = Math.Max(200, total - fixedWidth);
        _steps.Columns[2].Width = detailsWidth;
    }
}
