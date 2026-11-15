// -----------------------------------------------------------------------------
// A small progress window for the measuring run, built in code so it has no
// XAML of its own.
//
// The run happens on the UI thread, because the Revit API may only be touched
// from there. Report() therefore pumps the dispatcher queue after each update,
// which is what lets the window repaint and the Cancel button be clicked.
// -----------------------------------------------------------------------------

using System;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace PanelCableRoute.UI
{
    public class ProgressWindow : Window
    {
        private readonly TextBlock _text = new TextBlock();
        private readonly ProgressBar _bar = new ProgressBar();

        /// <summary>True once the user has pressed Cancel.</summary>
        public bool Cancelled { get; private set; }

        public ProgressWindow()
        {
            Title = "Measuring cable routes";
            Width = 460;
            SizeToContent = SizeToContentMode.Height;
            ResizeMode = ResizeMode.NoResize;
            WindowStartupLocation = WindowStartupLocation.CenterOwner;
            ShowInTaskbar = false;
            Background = new SolidColorBrush(Color.FromRgb(0xF1, 0xF4, 0xF9));
            FontFamily = new FontFamily("Segoe UI");
            FontSize = 12.0;

            StackPanel panel = new StackPanel();
            panel.Margin = new Thickness(20.0);

            _text.Text = "Starting...";
            _text.TextWrapping = TextWrapping.Wrap;
            _text.Margin = new Thickness(0.0, 0.0, 0.0, 10.0);
            _text.Foreground = new SolidColorBrush(
                Color.FromRgb(0x1E, 0x29, 0x3B));
            panel.Children.Add(_text);

            _bar.Height = 18.0;
            _bar.Minimum = 0.0;
            _bar.Maximum = 1.0;
            _bar.Value = 0.0;
            panel.Children.Add(_bar);

            Button cancel = new Button();
            cancel.Content = "Cancel";
            cancel.Width = 90.0;
            cancel.Height = 28.0;
            cancel.Margin = new Thickness(0.0, 14.0, 0.0, 0.0);
            cancel.HorizontalAlignment = HorizontalAlignment.Right;
            cancel.Click += delegate (object sender, RoutedEventArgs args)
            {
                Cancelled = true;
                _text.Text = "Stopping after this panel...";
            };
            panel.Children.Add(cancel);

            Content = panel;
        }

        /// <summary>
        /// Show where the run is up to. Returns false once Cancel has been
        /// pressed, which is what the core's progress callback wants.
        /// </summary>
        public bool Report(int done, int total, string panelName)
        {
            _text.Text = string.Format(CultureInfo.CurrentCulture,
                "Panel {0} of {1}: {2}", done, total,
                string.IsNullOrEmpty(panelName) ? "" : panelName);
            _bar.Value = total > 0 ? (double)done / total : 0.0;
            Pump();
            return !Cancelled;
        }

        /// <summary>
        /// Let WPF repaint and handle the Cancel click before the next panel
        /// is measured.
        /// </summary>
        private static void Pump()
        {
            try
            {
                Dispatcher.CurrentDispatcher.Invoke(
                    DispatcherPriority.Background,
                    new Action(delegate { }));
            }
            catch (Exception)
            {
                // Pumping is only for the look of the thing.
            }
        }
    }
}
