using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace RelayPairingAgent;

public enum AgentState
{
    NotConfigured,
    Ok,
    Error,
    Paused,
}

public class TrayAppContext : ApplicationContext
{
    private const string AutostartValueName = "RelayPairingAgent";
    private const string AutostartKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

    private readonly NotifyIcon _trayIcon;
    private readonly System.Windows.Forms.Timer _timer = new();
    private readonly PairingClient _client = new();
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _pauseItem;
    private readonly ToolStripMenuItem _autostartItem;

    private AgentConfig _config;
    private AgentState _state;
    private string _lastResult = "No update sent yet";
    private bool _paused;
    private bool _sendInFlight;

    public TrayAppContext()
    {
        _config = AgentConfig.Load();

        _statusItem = new ToolStripMenuItem(_lastResult) { Enabled = false };
        _pauseItem = new ToolStripMenuItem("Pause updates", null, OnTogglePause);
        _autostartItem = new ToolStripMenuItem("Start with Windows", null, OnToggleAutostart)
        {
            Checked = IsAutostartEnabled(),
        };

        var menu = new ContextMenuStrip();
        menu.Items.Add(_statusItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Send update now", null, OnSendNow));
        menu.Items.Add(_pauseItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Settings...", null, OnOpenSettings));
        menu.Items.Add(_autostartItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Exit", null, OnExit));

        _trayIcon = new NotifyIcon
        {
            ContextMenuStrip = menu,
            Visible = true,
        };
        _trayIcon.DoubleClick += OnOpenSettings;

        _timer.Tick += async (_, _) => await SendUpdateAsync();

        SetState(_config.IsConfigured ? AgentState.Ok : AgentState.NotConfigured,
            _config.IsConfigured ? "Starting..." : "Not configured: set relay URL and token");
        RestartTimer();

        if (_config.IsConfigured)
        {
            _ = SendUpdateAsync();
        }
        else
        {
            _trayIcon.ShowBalloonTip(5000, "Relay Pairing Agent",
                "Open Settings to set the relay URL and pairing token.", ToolTipIcon.Info);
        }
    }

    private void RestartTimer()
    {
        _timer.Stop();
        _timer.Interval = _config.ClampedIntervalSeconds * 1000;
        if (!_paused)
        {
            _timer.Start();
        }
    }

    private async Task SendUpdateAsync()
    {
        if (_paused || _sendInFlight)
        {
            return;
        }
        if (!_config.IsConfigured)
        {
            SetState(AgentState.NotConfigured, "Not configured: set relay URL and token");
            return;
        }

        _sendInFlight = true;
        try
        {
            await _client.SendPairingUpdateAsync(_config, CancellationToken.None);
            SetState(AgentState.Ok, $"Last update OK at {DateTime.Now:HH:mm:ss}");
        }
        catch (Exception exc)
        {
            SetState(AgentState.Error, $"Update failed at {DateTime.Now:HH:mm:ss}: {exc.Message}");
        }
        finally
        {
            _sendInFlight = false;
        }
    }

    private void SetState(AgentState state, string result)
    {
        _state = state;
        _lastResult = result;
        _statusItem.Text = Truncate(result, 100);

        var tooltipState = state switch
        {
            AgentState.Ok => "running",
            AgentState.Error => "error",
            AgentState.Paused => "paused",
            _ => "not configured",
        };
        UpdateTrayIcon(state);
        _trayIcon.Text = Truncate($"Relay Pairing Agent ({tooltipState})\n{result}", 127);
    }

    private void UpdateTrayIcon(AgentState state)
    {
        var color = state switch
        {
            AgentState.Ok => Color.FromArgb(46, 160, 67),
            AgentState.Error => Color.FromArgb(218, 54, 51),
            AgentState.Paused => Color.FromArgb(110, 118, 129),
            _ => Color.FromArgb(210, 153, 34),
        };

        var oldIcon = _trayIcon.Icon;
        _trayIcon.Icon = DrawStatusIcon(color);
        if (oldIcon is not null)
        {
            DestroyIcon(oldIcon.Handle);
            oldIcon.Dispose();
        }
    }

    private static Icon DrawStatusIcon(Color color)
    {
        using var bitmap = new Bitmap(32, 32);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            graphics.Clear(Color.Transparent);
            using var fill = new SolidBrush(color);
            graphics.FillEllipse(fill, 4, 4, 24, 24);
            using var pen = new Pen(Color.FromArgb(60, Color.Black), 2);
            graphics.DrawEllipse(pen, 4, 4, 24, 24);
        }
        var handle = bitmap.GetHicon();
        try
        {
            // Clone so the icon owns its data and the temporary handle can be released.
            return (Icon)Icon.FromHandle(handle).Clone();
        }
        finally
        {
            DestroyIcon(handle);
        }
    }

    private static string Truncate(string value, int maxLength) =>
        value.Length <= maxLength ? value : value[..(maxLength - 3)] + "...";

    private async void OnSendNow(object? sender, EventArgs e)
    {
        if (_paused)
        {
            OnTogglePause(sender, e);
            return;
        }
        await SendUpdateAsync();
    }

    private void OnTogglePause(object? sender, EventArgs e)
    {
        _paused = !_paused;
        _pauseItem.Checked = _paused;
        if (_paused)
        {
            _timer.Stop();
            SetState(AgentState.Paused, "Updates paused");
        }
        else
        {
            SetState(_config.IsConfigured ? AgentState.Ok : AgentState.NotConfigured, "Resuming...");
            RestartTimer();
            _ = SendUpdateAsync();
        }
    }

    private void OnOpenSettings(object? sender, EventArgs e)
    {
        using var form = new SettingsForm(_config);
        if (form.ShowDialog() != DialogResult.OK)
        {
            return;
        }
        form.ApplyTo(_config);
        try
        {
            _config.Save();
        }
        catch (Exception exc)
        {
            MessageBox.Show($"Could not save config: {exc.Message}", "Relay Pairing Agent",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        RestartTimer();
        if (!_paused)
        {
            _ = SendUpdateAsync();
        }
    }

    private void OnToggleAutostart(object? sender, EventArgs e)
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(AutostartKeyPath);
            if (_autostartItem.Checked)
            {
                key.DeleteValue(AutostartValueName, throwOnMissingValue: false);
                _autostartItem.Checked = false;
            }
            else
            {
                key.SetValue(AutostartValueName, $"\"{Application.ExecutablePath}\"");
                _autostartItem.Checked = true;
            }
        }
        catch (Exception exc)
        {
            MessageBox.Show($"Could not update autostart: {exc.Message}", "Relay Pairing Agent",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static bool IsAutostartEnabled()
    {
        using var key = Registry.CurrentUser.OpenSubKey(AutostartKeyPath);
        return key?.GetValue(AutostartValueName) is not null;
    }

    private void OnExit(object? sender, EventArgs e)
    {
        _timer.Stop();
        _trayIcon.Visible = false;
        _trayIcon.Dispose();
        _client.Dispose();
        Application.Exit();
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);
}
