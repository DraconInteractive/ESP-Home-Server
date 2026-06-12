using System.Net.Http.Headers;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace RelayPairingAgent;

public class PairingClient : IDisposable
{
    private readonly HttpClient _http;

    public PairingClient()
    {
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("RelayPairingAgent/0.1");
        _http.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
    }

    public async Task SendPairingUpdateAsync(AgentConfig config, CancellationToken cancellationToken)
    {
        var deviceId = CleanDeviceId(config.DeviceId);
        var url = $"{config.RelayUrl.TrimEnd('/')}/paired-devices/{deviceId}";

        var payload = new Dictionary<string, object>
        {
            ["name"] = config.Name.Trim().Length > 0 ? config.Name.Trim() : deviceId,
            ["type"] = config.Type.Trim().Length > 0 ? config.Type.Trim() : "windows-pc",
            ["hostname"] = Environment.MachineName,
            ["local_ips"] = DiscoverLocalIps(config.LocalIps),
            ["ports"] = SplitCsv(config.Ports),
            ["notes"] = config.Notes.Trim(),
        };

        using var request = new HttpRequestMessage(HttpMethod.Post, url)
        {
            Content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", config.PairingToken.Trim());

        using var response = await _http.SendAsync(request, cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(cancellationToken);
            var summary = body.Trim();
            if (summary.Length > 200)
            {
                summary = summary[..200];
            }
            throw new HttpRequestException($"relay returned {(int)response.StatusCode}: {summary}");
        }
    }

    public static string CleanDeviceId(string value)
    {
        var cleaned = new StringBuilder();
        foreach (var ch in value.Trim().ToLowerInvariant())
        {
            cleaned.Append(char.IsAsciiLetterOrDigit(ch) || ch is '-' or '_' or '.' ? ch : '-');
        }
        var result = cleaned.ToString().Trim('-');
        return result.Length > 0 ? result : "windows-pc";
    }

    public static List<string> SplitCsv(string value) =>
        value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).ToList();

    public static List<string> DiscoverLocalIps(string configuredCsv)
    {
        var configured = SplitCsv(configuredCsv);
        if (configured.Count > 0)
        {
            return configured;
        }

        var addresses = new SortedSet<string>(StringComparer.Ordinal);
        try
        {
            foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (nic.OperationalStatus != OperationalStatus.Up ||
                    nic.NetworkInterfaceType == NetworkInterfaceType.Loopback)
                {
                    continue;
                }
                foreach (var unicast in nic.GetIPProperties().UnicastAddresses)
                {
                    var address = unicast.Address;
                    if (address.AddressFamily is not (AddressFamily.InterNetwork or AddressFamily.InterNetworkV6))
                    {
                        continue;
                    }
                    var text = address.ToString();
                    if (text.StartsWith("127.") || text == "::1" ||
                        text.StartsWith("169.254.") || address.IsIPv6LinkLocal)
                    {
                        continue;
                    }
                    addresses.Add(text);
                }
            }
        }
        catch (NetworkInformationException)
        {
            // No network info available; send an empty list rather than failing the update.
        }
        return addresses.ToList();
    }

    public void Dispose() => _http.Dispose();
}
