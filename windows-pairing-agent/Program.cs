namespace RelayPairingAgent;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        using var mutex = new Mutex(initiallyOwned: true, "RelayPairingAgent-SingleInstance", out var isNewInstance);
        if (!isNewInstance)
        {
            MessageBox.Show("Relay Pairing Agent is already running. Check the system tray.",
                "Relay Pairing Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        ApplicationConfiguration.Initialize();
        Application.Run(new TrayAppContext());
    }
}
