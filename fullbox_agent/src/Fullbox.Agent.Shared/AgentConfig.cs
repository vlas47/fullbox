using System;

namespace Fullbox.Agent.Shared;

public sealed class AgentConfig
{
    public string AgentId { get; set; } = $"pc-{Guid.NewGuid():N}".Substring(0, 11);
    public string Name { get; set; } = Environment.MachineName;
    public string Host { get; set; } = Environment.MachineName;
    public string BaseUrl { get; set; } = "https://kondelyabr.ru";
    public string Token { get; set; } = "";
    public string PrintToken { get; set; } = "";
    public string PrintAgentName { get; set; } = "FullboxAgent";
    public int PingIntervalSec { get; set; } = 10;
    public int PollIntervalSec { get; set; } = 5;
    public int PrintPollIntervalSec { get; set; } = 2;
    public ComSettings Com { get; set; } = new();
}

public sealed class ComSettings
{
    public bool Enabled { get; set; } = true;
    public string PortName { get; set; } = "COM3";
    public int BaudRate { get; set; } = 9600;
    public ComEol Eol { get; set; } = ComEol.CrLf;
    public int IdleMs { get; set; } = 200;
}

public enum ComEol
{
    CrLf,
    Cr,
    Lf,
    Tab,
    None,
}
