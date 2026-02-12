using System;
using System.Collections.Generic;

namespace Fullbox.Agent.Shared;

public sealed class AgentPing
{
    public string AgentId { get; set; } = "";
    public string Name { get; set; } = "";
    public string Host { get; set; } = "";
    public string Version { get; set; } = "";
    public Dictionary<string, object> Meta { get; set; } = new();
}

public sealed class AgentCommand
{
    public long Id { get; set; }
    public string Command { get; set; } = "";
    public Dictionary<string, object> Payload { get; set; } = new();
    public DateTime CreatedAt { get; set; }
}

public sealed class AgentCommandAck
{
    public string AgentId { get; set; } = "";
    public bool Ok { get; set; } = true;
    public Dictionary<string, object> Result { get; set; } = new();
    public string Error { get; set; } = "";
}

public sealed class AgentEvent
{
    public string AgentId { get; set; } = "";
    public string EventType { get; set; } = "";
    public Dictionary<string, object> Payload { get; set; } = new();
}
