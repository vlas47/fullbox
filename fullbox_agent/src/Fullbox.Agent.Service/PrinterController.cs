using System.Printing;

namespace Fullbox.Agent.Service;

public sealed class PrinterController
{
    public IReadOnlyList<string> ListPrinters()
    {
        var server = new LocalPrintServer();
        return server.GetPrintQueues().Select(q => q.Name).ToList();
    }

    public bool Pause(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return false;
        queue.Pause();
        queue.Commit();
        return true;
    }

    public bool Resume(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return false;
        queue.Resume();
        queue.Commit();
        return true;
    }

    public bool Clear(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return false;
        queue.Purge();
        queue.Commit();
        return true;
    }

    public Dictionary<string, object> Status(string printerName)
    {
        var queue = GetQueue(printerName);
        if (queue == null) return new Dictionary<string, object> { ["found"] = false };
        queue.Refresh();
        return new Dictionary<string, object>
        {
            ["found"] = true,
            ["name"] = queue.Name,
            ["is_paused"] = queue.IsPaused,
            ["is_offline"] = queue.IsOffline,
            ["is_busy"] = queue.IsBusy,
            ["jobs"] = queue.NumberOfJobs,
        };
    }

    private static PrintQueue? GetQueue(string printerName)
    {
        if (string.IsNullOrWhiteSpace(printerName))
        {
            return null;
        }
        var server = new LocalPrintServer();
        return server.GetPrintQueue(printerName);
    }
}
