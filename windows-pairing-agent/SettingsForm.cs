namespace RelayPairingAgent;

public class SettingsForm : Form
{
    private readonly TextBox _relayUrl = new();
    private readonly TextBox _pairingToken = new() { UseSystemPasswordChar = true };
    private readonly TextBox _deviceId = new();
    private readonly TextBox _name = new();
    private readonly TextBox _type = new();
    private readonly TextBox _ports = new();
    private readonly TextBox _localIps = new();
    private readonly TextBox _notes = new();
    private readonly NumericUpDown _intervalSeconds = new() { Minimum = 60, Maximum = 86400, Increment = 60 };

    public SettingsForm(AgentConfig config)
    {
        Text = "Relay Pairing Agent Settings";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Font;
        ClientSize = new Size(440, 360);

        _relayUrl.Text = config.RelayUrl;
        _pairingToken.Text = config.PairingToken;
        _deviceId.Text = config.DeviceId;
        _name.Text = config.Name;
        _type.Text = config.Type;
        _ports.Text = config.Ports;
        _localIps.Text = config.LocalIps;
        _notes.Text = config.Notes;
        _intervalSeconds.Value = Math.Clamp(config.IntervalSeconds, 60, 86400);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            Padding = new Padding(10),
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 140));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

        AddRow(layout, "Relay URL", _relayUrl);
        AddRow(layout, "Pairing token", _pairingToken);
        AddRow(layout, "Device ID", _deviceId);
        AddRow(layout, "Name", _name);
        AddRow(layout, "Type", _type);
        AddRow(layout, "Ports (csv)", _ports);
        AddRow(layout, "Local IPs (csv)", _localIps);
        AddRow(layout, "Notes", _notes);
        AddRow(layout, "Interval (seconds)", _intervalSeconds);

        var buttons = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
        };
        var save = new Button { Text = "Save", DialogResult = DialogResult.OK };
        var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel };
        buttons.Controls.Add(save);
        buttons.Controls.Add(cancel);
        layout.Controls.Add(new Label(), 0, layout.RowCount);
        layout.Controls.Add(buttons, 1, layout.RowCount);
        layout.RowCount++;

        AcceptButton = save;
        CancelButton = cancel;
        Controls.Add(layout);
    }

    private static void AddRow(TableLayoutPanel layout, string labelText, Control control)
    {
        var label = new Label
        {
            Text = labelText,
            TextAlign = ContentAlignment.MiddleLeft,
            Dock = DockStyle.Fill,
        };
        control.Dock = DockStyle.Fill;
        layout.Controls.Add(label, 0, layout.RowCount);
        layout.Controls.Add(control, 1, layout.RowCount);
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 32));
        layout.RowCount++;
    }

    public void ApplyTo(AgentConfig config)
    {
        config.RelayUrl = _relayUrl.Text.Trim();
        config.PairingToken = _pairingToken.Text.Trim();
        config.DeviceId = PairingClient.CleanDeviceId(_deviceId.Text);
        config.Name = _name.Text.Trim();
        config.Type = _type.Text.Trim();
        config.Ports = _ports.Text.Trim();
        config.LocalIps = _localIps.Text.Trim();
        config.Notes = _notes.Text.Trim();
        config.IntervalSeconds = (int)_intervalSeconds.Value;
    }
}
