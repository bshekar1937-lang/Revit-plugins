// -----------------------------------------------------------------------------
// The setup dialog: scope, cable figures and the tray tolerance, before
// anything is measured.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using PanelCableRoute.Core;

namespace PanelCableRoute.UI
{
    public partial class SetupWindow : Window
    {
        /// <summary>One line of the panel list.</summary>
        private class PanelChoice
        {
            public long Id;
            public string Display = "";
            public string Search = "";

            public override string ToString()
            {
                return Display;
            }
        }

        private readonly List<PanelChoice> _choices = new List<PanelChoice>();
        private readonly HashSet<long> _selected = new HashSet<long>();
        private bool _ready;
        private bool _updatingList;

        /// <summary>Filled in when the user presses Measure routes.</summary>
        public RouteOptions Options { get; private set; }

        public SetupWindow(
            RouteOptions remembered, List<EquipmentItem> panels,
            int isolatorCount, int trayCount)
        {
            InitializeComponent();

            PairsCheck.IsChecked = remembered.MeasurePanelPairs;
            CircuitsCheck.IsChecked = remembered.MeasureCircuits;
            IsolatorsCheck.IsChecked = remembered.IncludeIsolators;
            DrawCheck.IsChecked = remembered.DrawDetailLines;
            AllowanceBox.Text = Text(remembered.AllowancePercent);
            MvAmBox.Text = Text(remembered.MilliVoltsPerAmpMetre);
            LimitBox.Text = Text(remembered.LimitPercent);
            CurrentBox.Text = Text(remembered.Current);
            VoltageBox.Text = Text(remembered.Voltage);
            ToleranceBox.Text = Text(remembered.ToleranceMm);
            BridgeBox.Text = Text(remembered.MaxBridgeMm);
            PreferenceBox.Text = Text(remembered.TrayPreference);
            MaxPairBox.Text = Text(remembered.MaxPairMetres);

            for (int index = 0; index < panels.Count; index++)
            {
                EquipmentItem panel = panels[index];
                PanelChoice choice = new PanelChoice();
                choice.Id = panel.Id;
                choice.Display = panel.Name
                    + (string.IsNullOrEmpty(panel.LevelName)
                        ? "" : "   -   " + panel.LevelName)
                    + "   (ID " + panel.Id.ToString(
                        CultureInfo.InvariantCulture) + ")";
                choice.Search = choice.Display.ToLowerInvariant();
                _choices.Add(choice);
            }
            AllPanelsText.Text = "All " + panels.Count.ToString() + " panels";
            ModelText.Text =
                panels.Count.ToString() + " electrical equipment item(s) that "
                + "can act as a panel." + Environment.NewLine
                + isolatorCount.ToString() + " isolator(s) found by Comments, "
                + "JAC-EL-Rating or family name." + Environment.NewLine
                + trayCount.ToString() + " cable tray(s) to build the network "
                + "from.";

            FillPanelList();
            _ready = true;
            UpdateScopeText();
            UpdateSummary();
        }

        private static string Text(double value)
        {
            return value.ToString("0.###", CultureInfo.InvariantCulture);
        }

        // ----- the panel list -------------------------------------------------
        private void FillPanelList()
        {
            _updatingList = true;
            try
            {
                string filter = (PanelFilterBox.Text == null
                    ? "" : PanelFilterBox.Text.Trim().ToLowerInvariant());
                PanelList.Items.Clear();
                for (int index = 0; index < _choices.Count; index++)
                {
                    PanelChoice choice = _choices[index];
                    if (filter.Length > 0 && choice.Search.IndexOf(
                            filter, StringComparison.Ordinal) < 0)
                    {
                        continue;
                    }
                    PanelList.Items.Add(choice);
                    if (_selected.Contains(choice.Id))
                    {
                        PanelList.SelectedItems.Add(choice);
                    }
                }
            }
            finally
            {
                _updatingList = false;
            }
        }

        private void OnPanelFilterChanged(object sender, TextChangedEventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            FillPanelList();
        }

        private void OnPanelSelectionChanged(
            object sender, SelectionChangedEventArgs args)
        {
            if (_updatingList)
            {
                return;
            }
            // Only the panels on screen can change, so add and remove rather
            // than rebuilding the whole set from the visible rows.
            for (int index = 0; index < args.RemovedItems.Count; index++)
            {
                PanelChoice choice = args.RemovedItems[index] as PanelChoice;
                if (choice != null)
                {
                    _selected.Remove(choice.Id);
                }
            }
            for (int index = 0; index < args.AddedItems.Count; index++)
            {
                PanelChoice choice = args.AddedItems[index] as PanelChoice;
                if (choice != null)
                {
                    _selected.Add(choice.Id);
                }
            }
            if (_selected.Count > 0 && AllPanelsCheck.IsChecked == true)
            {
                AllPanelsCheck.IsChecked = false;   // raises OnAllPanelsChanged
                return;
            }
            UpdateScopeText();
            UpdateSummary();
        }

        private void OnAllPanelsChanged(object sender, RoutedEventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            bool all = AllPanelsCheck.IsChecked == true;
            PanelFilterBox.IsEnabled = !all;
            PanelList.IsEnabled = !all;
            if (all)
            {
                _selected.Clear();
                _updatingList = true;
                try
                {
                    PanelList.SelectedItems.Clear();
                }
                finally
                {
                    _updatingList = false;
                }
            }
            UpdateScopeText();
            UpdateSummary();
        }

        private int PanelsInScope()
        {
            return AllPanelsCheck.IsChecked == true
                ? _choices.Count : _selected.Count;
        }

        private void UpdateScopeText()
        {
            if (AllPanelsCheck.IsChecked == true)
            {
                ScopeText.Text = "Every panel in the model will be measured. "
                    + "Untick to choose from the list.";
                return;
            }
            if (_selected.Count == 0)
            {
                ScopeText.Text = "No panels chosen yet - pick them in the list "
                    + "above, or tick All panels.";
                return;
            }
            ScopeText.Text = _selected.Count.ToString() + " of "
                + _choices.Count.ToString() + " panel(s) chosen.";
        }

        // ----- live summary ---------------------------------------------------
        private void OnInputChanged(object sender, TextChangedEventArgs args)
        {
            UpdateSummary();
        }

        private void UpdateSummary()
        {
            if (!_ready)
            {
                return;
            }
            int count = PanelsInScope();
            long pairs = PairsCheck.IsChecked == true
                ? (long)count * (count - 1) / 2 : 0;
            double allowance;
            double current;
            double voltage;
            double mvam;
            double limit;
            if (!TryRead(AllowanceBox, out allowance)
                || !TryRead(CurrentBox, out current)
                || !TryRead(VoltageBox, out voltage)
                || !TryRead(MvAmBox, out mvam)
                || !TryRead(LimitBox, out limit))
            {
                SummaryText.Text = "Enter the allowance, mV/A/m, limit, "
                    + "current and voltage as numbers to preview the "
                    + "calculation.";
                return;
            }
            if (voltage <= 0.0)
            {
                SummaryText.Text = "The voltage must be more than zero.";
                return;
            }
            double example = 100.0 * (1.0 + allowance / 100.0);
            double drop = VoltageDrop.Volts(example, current, mvam);
            double percent = drop / voltage * 100.0;
            double reach = VoltageDrop.ReachMetres(
                voltage, limit, current, mvam);
            string text = string.Format(CultureInfo.InvariantCulture,
                "{0} panel pair route(s) to measure. A 100 m route becomes "
                + "{1:0.0} m with allowance and drops {2:0.00} V, which is "
                + "{3:0.00} % of {4:0} V.",
                pairs, example, drop, percent, voltage);
            if (reach > 0.0)
            {
                text += string.Format(CultureInfo.InvariantCulture,
                    " At {0:0.##} A on this cable the {1:0.##} % limit is "
                    + "reached at about {2:0} m of cable.",
                    current, limit, reach);
            }
            SummaryText.Text = text;
        }

        private static bool TryRead(TextBox box, out double value)
        {
            return double.TryParse(
                box.Text == null ? "" : box.Text.Trim(),
                NumberStyles.Float, CultureInfo.InvariantCulture, out value)
                && !double.IsNaN(value) && !double.IsInfinity(value);
        }

        private double Read(TextBox box, string label, double minimum)
        {
            double value;
            if (!TryRead(box, out value))
            {
                throw new ArgumentException(label + " must be a number.");
            }
            if (value < minimum)
            {
                throw new ArgumentException(string.Format(
                    CultureInfo.InvariantCulture,
                    "{0} must be {1} or more.", label, minimum));
            }
            return value;
        }

        // ----- buttons --------------------------------------------------------
        private void OnRun(object sender, RoutedEventArgs args)
        {
            try
            {
                if (PairsCheck.IsChecked != true
                    && CircuitsCheck.IsChecked != true
                    && IsolatorsCheck.IsChecked != true)
                {
                    throw new ArgumentException(
                        "Choose at least one thing to measure: panel pairs, "
                        + "circuit equipment or isolators.");
                }
                if (AllPanelsCheck.IsChecked != true && _selected.Count == 0)
                {
                    throw new ArgumentException(
                        "Choose the panels to measure, or tick All panels.");
                }
                RouteOptions options = new RouteOptions();
                options.MeasurePanelPairs = PairsCheck.IsChecked == true;
                options.MeasureCircuits = CircuitsCheck.IsChecked == true;
                options.IncludeIsolators = IsolatorsCheck.IsChecked == true;
                options.DrawDetailLines = DrawCheck.IsChecked == true;
                options.AllowancePercent = Read(
                    AllowanceBox, "Cable allowance", 0.0);
                options.MilliVoltsPerAmpMetre = Read(
                    MvAmBox, "Cable mV/A/m", 0.0);
                options.LimitPercent = Read(
                    LimitBox, "Voltage drop limit", 0.0);
                options.Current = Read(CurrentBox, "Current", 0.0);
                options.Voltage = Read(VoltageBox, "Voltage", 1.0);
                options.ToleranceMm = Read(
                    ToleranceBox, "Tray connection tolerance", 1.0);
                options.MaxPairMetres = Read(
                    MaxPairBox, "Pair length limit", 0.0);
                options.MaxBridgeMm = Read(
                    BridgeBox, "Tray gap joining distance", 0.0);
                options.TrayPreference = Read(
                    PreferenceBox, "Tray preference", 1.0);
                if (AllPanelsCheck.IsChecked != true)
                {
                    options.PanelIds = new HashSet<long>(_selected);
                }
                Options = options;
                DialogResult = true;
                Close();
            }
            catch (ArgumentException error)
            {
                MessageBox.Show(this, error.Message, "Panel Cable Route Pro",
                    MessageBoxButton.OK, MessageBoxImage.Warning);
            }
        }

        private void OnCancel(object sender, RoutedEventArgs args)
        {
            Options = null;
            DialogResult = false;
            Close();
        }
    }
}
