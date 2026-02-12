using System.Drawing;
using System.Drawing.Printing;
using Microsoft.Extensions.Logging;

namespace Fullbox.Agent.Service;

public sealed class PrintJobRunner
{
    private readonly AgentRuntime _runtime;
    private readonly PrintAgentClient _client;
    private readonly ILogger<PrintJobRunner> _logger;

    public PrintJobRunner(AgentRuntime runtime, PrintAgentClient client, ILogger<PrintJobRunner> logger)
    {
        _runtime = runtime;
        _client = client;
        _logger = logger;
    }

    public async Task RunAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            _runtime.ReloadConfig();
            _client.Reload();

            if (string.IsNullOrWhiteSpace(_runtime.Config.PrintToken))
            {
                await Task.Delay(TimeSpan.FromSeconds(5), token);
                continue;
            }

            var response = await _client.GetNextJobAsync(token);
            if (response == null)
            {
                await Task.Delay(TimeSpan.FromSeconds(2), token);
                continue;
            }
            if (response.Paused || !response.HasJob || response.Job == null)
            {
                await Task.Delay(TimeSpan.FromSeconds(_runtime.Config.PrintPollIntervalSec), token);
                continue;
            }

            var job = response.Job;
            var success = false;
            string error = "";
            try
            {
                success = PrintLabel(job);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Print failed");
                error = ex.Message;
            }

            await _client.CompleteJobAsync(
                job.Id,
                success ? "printed" : "failed",
                success ? "" : (string.IsNullOrWhiteSpace(error) ? "print_failed" : error),
                token
            );
        }
    }

    private bool PrintLabel(PrintJob job)
    {
        if (string.IsNullOrWhiteSpace(job.PrinterName))
        {
            return false;
        }
        var imageBytes = Convert.FromBase64String(job.LabelPngBase64);
        using var stream = new MemoryStream(imageBytes);
        using var image = Image.FromStream(stream);

        using var doc = new PrintDocument();
        doc.PrinterSettings.PrinterName = job.PrinterName;
        if (!doc.PrinterSettings.IsValid)
        {
            return false;
        }
        var width = MmToHundredths(job.LabelWidthMm);
        var height = MmToHundredths(job.LabelHeightMm);
        doc.DefaultPageSettings.PaperSize = new PaperSize("Label", width, height);
        doc.DefaultPageSettings.Margins = new Margins(0, 0, 0, 0);
        doc.PrintPage += (_, args) =>
        {
            args.Graphics.DrawImage(image, args.PageBounds);
            args.HasMorePages = false;
        };
        doc.Print();
        return true;
    }

    private static int MmToHundredths(int mm)
    {
        var inches = mm / 25.4;
        return Math.Max(1, (int)Math.Round(inches * 100));
    }
}
