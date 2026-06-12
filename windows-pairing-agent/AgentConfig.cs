using System.Text.Json;
using System.Text.Json.Serialization;

namespace RelayPairingAgent;

public class AgentConfig
{
    [JsonPropertyName("relay_url")]
    public string RelayUrl { get; set; } = "https://relay.dracon.au";

    [JsonPropertyName("pairing_token")]
    public string PairingToken { get; set; } = "";

    [JsonPropertyName("device_id")]
    public string DeviceId { get; set; } = Environment.MachineName.ToLowerInvariant();

    [JsonPropertyName("name")]
    public string Name { get; set; } = Environment.MachineName;

    [JsonPropertyName("type")]
    public string Type { get; set; } = "windows-pc";

    [JsonPropertyName("ports")]
    public string Ports { get; set; } = "rdp:3389";

    [JsonPropertyName("notes")]
    public string Notes { get; set; } = "";

    [JsonPropertyName("local_ips")]
    public string LocalIps { get; set; } = "";

    [JsonPropertyName("interval_seconds")]
    public int IntervalSeconds { get; set; } = 300;

    [JsonIgnore]
    public bool IsConfigured => RelayUrl.Trim().Length > 0 && PairingToken.Trim().Length > 0;

    public int ClampedIntervalSeconds => Math.Max(60, IntervalSeconds);

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
    };

    public static string ConfigDirectory =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "RelayPairingAgent");

    public static string ConfigPath => Path.Combine(ConfigDirectory, "config.json");

    public static AgentConfig Load()
    {
        try
        {
            if (File.Exists(ConfigPath))
            {
                var parsed = JsonSerializer.Deserialize<AgentConfig>(File.ReadAllText(ConfigPath));
                if (parsed is not null)
                {
                    return parsed;
                }
            }
        }
        catch (Exception)
        {
            // Fall through to defaults; a corrupt config should not stop the agent.
        }
        return new AgentConfig();
    }

    public void Save()
    {
        Directory.CreateDirectory(ConfigDirectory);
        File.WriteAllText(ConfigPath, JsonSerializer.Serialize(this, JsonOptions));
    }
}
