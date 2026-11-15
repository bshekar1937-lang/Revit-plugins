// -----------------------------------------------------------------------------
// The results panel: the 3D preview, the route table, the panel summary and
// the issues, with the draw and export buttons.
//
// Selecting elements in Revit is handed back to the command through the
// SelectInRevit callback, so this window never touches the Revit API itself.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Media3D;
using PanelCableRoute.Core;

namespace PanelCableRoute.UI
{
    /// <summary>What the window was closed with.</summary>
    public enum ResultChoice
    {
        Close,
        Draw
    }

    /// <summary>One row of the route table.</summary>
    public class RouteRow : INotifyPropertyChanged
    {
        private readonly RouteRecord _record;

        public RouteRow(RouteRecord record)
        {
            _record = record;
        }

        public RouteRecord Record
        {
            get { return _record; }
        }

        public bool Include
        {
            get { return _record.Include; }
            set
            {
                if (_record.Include == value)
                {
                    return;
                }
                _record.Include = value;
                Changed("Include");
                if (IncludeChanged != null)
                {
                    IncludeChanged(this, EventArgs.Empty);
                }
            }
        }

        /// <summary>Raised when the tick box changes, so totals can refresh.</summary>
        public event EventHandler IncludeChanged;

        public string SourceName { get { return _record.Source.Name; } }
        public string DestinationName { get { return _record.Destination.Name; } }
        public string KindText { get { return RouteKindText.Of(_record.Kind); } }
        public string CircuitNumber { get { return _record.CircuitNumber; } }
        public double TrayMetres
        {
            get { return Units.FeetToMetres(_record.TrayLength); }
        }
        public double EndMetres
        {
            get
            {
                return Units.FeetToMetres(
                    _record.SourceGap + _record.DestinationGap);
            }
        }
        public double BridgeMetres
        {
            get { return Units.FeetToMetres(_record.Bridge); }
        }
        public double LengthMetres
        {
            get { return Units.FeetToMetres(_record.WithAllowance); }
        }
        public double Current { get { return _record.Current; } }
        public double Voltage { get { return _record.Voltage; } }
        public double DropVolts { get { return _record.DropVolts; } }
        public double DropPercent { get { return _record.DropPercent; } }
        public bool WithinLimit { get { return _record.WithinLimit; } }
        public string Status { get { return _record.WithinLimit ? "OK" : "OVER"; } }

        /// <summary>Everything the filter box looks through.</summary>
        public string SearchText
        {
            get
            {
                return (SourceName + " " + DestinationName + " "
                    + _record.Source.LevelName + " "
                    + _record.Destination.LevelName + " "
                    + CircuitNumber + " " + _record.LoadName + " "
                    + KindText + " "
                    + _record.Source.Id.ToString(CultureInfo.InvariantCulture)
                    + " "
                    + _record.Destination.Id.ToString(
                        CultureInfo.InvariantCulture)).ToLowerInvariant();
            }
        }

        public event PropertyChangedEventHandler PropertyChanged;

        private void Changed(string name)
        {
            if (PropertyChanged != null)
            {
                PropertyChanged(this, new PropertyChangedEventArgs(name));
            }
        }
    }

    /// <summary>One row of the panel summary.</summary>
    public class PanelRow
    {
        public PanelRow(PanelSummary summary)
        {
            Name = summary.Name;
            LevelName = summary.Panel.LevelName;
            Id = summary.Panel.Id;
            RouteCount = summary.RouteCount;
            LongestMetres = Units.FeetToMetres(summary.Longest);
            WorstPercent = summary.WorstPercent;
            OverLimit = summary.OverLimit;
            Bridged = summary.Bridged;
        }

        public string Name { get; private set; }
        public string LevelName { get; private set; }
        public long Id { get; private set; }
        public int RouteCount { get; private set; }
        public double LongestMetres { get; private set; }
        public double WorstPercent { get; private set; }
        public int OverLimit { get; private set; }
        public int Bridged { get; private set; }
        public bool HasOverLimit { get { return OverLimit > 0; } }
    }

    /// <summary>One line of the issues list.</summary>
    public class IssueRow
    {
        public string Text;
        public long ElementId = -1;

        public override string ToString()
        {
            return Text;
        }
    }

    public partial class ResultWindow : Window
    {
        private readonly RouteResult _result;
        private readonly RouteOptions _options;
        private readonly List<TraySegment> _trays;
        private readonly int _nodeCount;
        private readonly int _fittingsBridged;
        private readonly ObservableCollection<RouteRow> _rows =
            new ObservableCollection<RouteRow>();
        private ICollectionView _view;
        private RouteRecord _picked;
        private bool _ready;

        // 3D view state
        private readonly PerspectiveCamera _camera = new PerspectiveCamera();
        private double _azimuth = 35.0;
        private double _elevation = 26.0;
        private double _distance = 24.0;
        private Vector3D _target = new Vector3D(0.0, 0.0, 0.0);
        private string _dragMode;
        private Point _dragOrigin;

        /// <summary>Close / Draw, read by the command after ShowDialog.</summary>
        public ResultChoice Choice { get; private set; }

        /// <summary>Files written by the export buttons.</summary>
        public List<string> Exported { get; private set; }

        /// <summary>
        /// Called with the element ids to select, and whether to zoom. The
        /// command supplies this, so the window needs no Revit reference.
        /// </summary>
        public Func<List<long>, bool, string> SelectInRevit { get; set; }

        public ResultWindow(
            RouteResult result, RouteOptions options,
            List<TraySegment> trays, int nodeCount, int fittingsBridged)
        {
            InitializeComponent();
            _result = result;
            _options = options;
            _trays = trays;
            _nodeCount = nodeCount;
            _fittingsBridged = fittingsBridged;
            Choice = ResultChoice.Close;
            Exported = new List<string>();

            _camera.FieldOfView = 45.0;
            Viewport.Camera = _camera;

            for (int index = 0; index < result.Routes.Count; index++)
            {
                RouteRow row = new RouteRow(result.Routes[index]);
                row.IncludeChanged += OnRowIncludeChanged;
                _rows.Add(row);
            }
            _view = CollectionViewSource.GetDefaultView(_rows);
            _view.Filter = RowVisible;
            RouteGrid.ItemsSource = _view;

            List<PanelSummary> summaries = result.PanelRows();
            List<PanelRow> panelRows = new List<PanelRow>();
            for (int index = 0; index < summaries.Count; index++)
            {
                panelRows.Add(new PanelRow(summaries[index]));
            }
            PanelGrid.ItemsSource = panelRows;

            FillIssues();
            DrawButton.IsEnabled = result.Routes.Count > 0;
            _ready = true;
            RefreshHeaders();
            RefreshStatus();
            UpdateBanner(null, true);
            BuildScene();
        }

        // ----- text -----------------------------------------------------------
        private void RefreshHeaders()
        {
            int over = 0;
            for (int index = 0; index < _result.Routes.Count; index++)
            {
                if (!_result.Routes[index].WithinLimit)
                {
                    over++;
                }
            }
            Tab3d.Header = "3D preview";
            TabRoutes.Header = "Routes (" + _result.Routes.Count.ToString() + ")";
            TabPanels.Header = "Panels (" + _result.Panels.Count.ToString() + ")";
            TabIssues.Header = "Overview and issues ("
                + _result.Unreachable.Count.ToString() + ")";
            HeadlineText.Text = string.Format(CultureInfo.InvariantCulture,
                "{0} route(s) from {1} panel(s) - {2} over the {3:0.##} % "
                + "limit. Nothing is drawn in the model until you ask.",
                _result.Routes.Count, _result.Panels.Count, over,
                _options.LimitPercent);
            OverviewText.Text = string.Join(Environment.NewLine,
                Export.Report(_result, _options, _trays.Count, _nodeCount,
                    _fittingsBridged).ToArray());
        }

        private int TickedCount()
        {
            int count = 0;
            for (int index = 0; index < _rows.Count; index++)
            {
                if (_rows[index].Include)
                {
                    count++;
                }
            }
            return count;
        }

        private void RefreshStatus()
        {
            int shown = 0;
            foreach (object item in _view)
            {
                shown++;
            }
            RouteStatusText.Text = string.Format(CultureInfo.InvariantCulture,
                "Showing {0} of {1} route(s); {2} ticked. Click a column "
                + "header to sort, a row to pick it out in the 3D preview.",
                shown, _rows.Count, TickedCount());
        }

        private void UpdateBanner(string message, bool good)
        {
            if (message == null)
            {
                int over = 0;
                for (int index = 0; index < _result.Routes.Count; index++)
                {
                    if (!_result.Routes[index].WithinLimit)
                    {
                        over++;
                    }
                }
                if (_result.Routes.Count == 0)
                {
                    message = "No routes were measured.";
                    good = false;
                }
                else if (over > 0)
                {
                    message = string.Format(CultureInfo.InvariantCulture,
                        "{0} of {1} route(s) are over the {2:0.##} % limit. "
                        + "{3} route(s) ticked for drawing and export.",
                        over, _result.Routes.Count, _options.LimitPercent,
                        TickedCount());
                    good = false;
                }
                else
                {
                    message = string.Format(CultureInfo.InvariantCulture,
                        "Every route is within the {0:0.##} % limit. {1} "
                        + "route(s) ticked for drawing and export.",
                        _options.LimitPercent, TickedCount());
                    good = true;
                }
            }
            BannerBorder.Background = new SolidColorBrush(good
                ? Color.FromRgb(0xF0, 0xFD, 0xF4)
                : Color.FromRgb(0xFE, 0xF2, 0xF2));
            BannerBorder.BorderBrush = new SolidColorBrush(good
                ? Color.FromRgb(0xBB, 0xF7, 0xD0)
                : Color.FromRgb(0xFC, 0xA5, 0xA5));
            BannerText.Foreground = new SolidColorBrush(good
                ? Color.FromRgb(0x16, 0x65, 0x34)
                : Color.FromRgb(0xB9, 0x1C, 0x1C));
            BannerText.Text = message;
        }

        private void FillIssues()
        {
            IssueList.Items.Clear();
            for (int index = 0; index < _result.Unreachable.Count; index++)
            {
                RouteProblem problem = _result.Unreachable[index];
                IssueRow row = new IssueRow();
                row.ElementId = problem.Item.Id;
                row.Text = problem.Item.Name + " - " + problem.Reason
                    + "  (ID " + problem.Item.Id.ToString(
                        CultureInfo.InvariantCulture) + ")";
                IssueList.Items.Add(row);
            }
            for (int index = 0; index < _result.Routes.Count; index++)
            {
                RouteRecord route = _result.Routes[index];
                if (route.Bridge <= Geometry.Epsilon)
                {
                    continue;
                }
                IssueRow row = new IssueRow();
                row.ElementId = route.Source.Id;
                row.Text = string.Format(CultureInfo.InvariantCulture,
                    "{0}: the modelled trays do not join up - {1:0.00} m of "
                    + "the route is a straight bridge between tray ends.",
                    route.Title, Units.FeetToMetres(route.Bridge));
                IssueList.Items.Add(row);
            }
            if (IssueList.Items.Count == 0)
            {
                IssueRow row = new IssueRow();
                row.Text = "Every item in scope was reached through the "
                    + "modelled trays.";
                IssueList.Items.Add(row);
            }
        }

        // ----- filtering and ticking -------------------------------------------
        private bool RowVisible(object item)
        {
            RouteRow row = item as RouteRow;
            if (row == null)
            {
                return false;
            }
            if (OnlyOverCheck.IsChecked == true && row.WithinLimit)
            {
                return false;
            }
            string filter = FilterBox.Text == null
                ? "" : FilterBox.Text.Trim().ToLowerInvariant();
            if (filter.Length == 0)
            {
                return true;
            }
            return row.SearchText.IndexOf(filter, StringComparison.Ordinal) >= 0;
        }

        private void OnFilterChanged(object sender, TextChangedEventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            _view.Refresh();
            RefreshStatus();
            BuildScene();
        }

        private void OnFilterToggled(object sender, RoutedEventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            _view.Refresh();
            RefreshStatus();
            BuildScene();
        }

        private void OnRowIncludeChanged(object sender, EventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            RefreshStatus();
            UpdateBanner(null, true);
        }

        private void SetTicks(bool state)
        {
            foreach (object item in _view)
            {
                RouteRow row = item as RouteRow;
                if (row != null)
                {
                    row.Include = state;
                }
            }
            RefreshStatus();
            UpdateBanner(null, true);
        }

        private void OnTickAll(object sender, RoutedEventArgs args)
        {
            SetTicks(true);
        }

        private void OnTickNone(object sender, RoutedEventArgs args)
        {
            SetTicks(false);
        }

        private void OnRouteSelected(object sender, SelectionChangedEventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            RouteRow row = RouteGrid.SelectedItem as RouteRow;
            _picked = row == null ? null : row.Record;
            BuildScene();
            if (_picked != null)
            {
                UpdateBanner(string.Format(CultureInfo.InvariantCulture,
                    "Picked {0}: {1:0.00} m, {2:0.00} V drop ({3:0.00} %). It "
                    + "is highlighted in the 3D preview.",
                    _picked.Title, Units.FeetToMetres(_picked.WithAllowance),
                    _picked.DropVolts, _picked.DropPercent),
                    _picked.WithinLimit);
            }
        }

        // ----- Revit selection --------------------------------------------------
        private void Focus(List<long> ids, bool zoom)
        {
            if (SelectInRevit == null || ids.Count == 0)
            {
                return;
            }
            string message = SelectInRevit(ids, zoom);
            UpdateBanner(message, true);
        }

        private void OnSelectInRevit(object sender, RoutedEventArgs args)
        {
            RouteRow row = RouteGrid.SelectedItem as RouteRow;
            if (row == null)
            {
                UpdateBanner("Pick a route in the table first.", false);
                return;
            }
            Focus(new List<long>
            {
                row.Record.Source.Id, row.Record.Destination.Id
            }, false);
        }

        private void OnZoomInRevit(object sender, RoutedEventArgs args)
        {
            RouteRow row = RouteGrid.SelectedItem as RouteRow;
            if (row == null)
            {
                UpdateBanner("Pick a route in the table first.", false);
                return;
            }
            Focus(new List<long>
            {
                row.Record.Source.Id, row.Record.Destination.Id
            }, true);
        }

        private void OnPanelDoubleClick(object sender, MouseButtonEventArgs args)
        {
            PanelRow row = PanelGrid.SelectedItem as PanelRow;
            if (row != null)
            {
                Focus(new List<long> { row.Id }, true);
            }
        }

        private void OnSelectIssue(object sender, RoutedEventArgs args)
        {
            IssueRow row = IssueList.SelectedItem as IssueRow;
            if (row == null || row.ElementId < 0)
            {
                UpdateBanner("Pick an item in the list first.", false);
                return;
            }
            Focus(new List<long> { row.ElementId }, true);
        }

        // ----- 3D preview -------------------------------------------------------
        private void OnSceneOptionChanged(object sender, RoutedEventArgs args)
        {
            if (!_ready)
            {
                return;
            }
            BuildScene();
        }

        private void BuildScene()
        {
            try
            {
                Viewport.Children.Clear();
                List<RouteRecord> visible = new List<RouteRecord>();
                foreach (object item in _view)
                {
                    RouteRow row = item as RouteRow;
                    if (row != null)
                    {
                        visible.Add(row.Record);
                    }
                }
                SceneOptions options = new SceneOptions();
                options.ShowTrays = ShowTraysCheck.IsChecked == true;
                options.ShowPanels = ShowPanelsCheck.IsChecked == true;
                options.ShowAllRoutes = ShowAllRoutesCheck.IsChecked == true;
                options.Picked = _picked;
                Scene scene = Scene3D.Build(visible, _trays, options);
                ModelVisual3D visual = new ModelVisual3D();
                visual.Content = scene.Content;
                Viewport.Children.Add(visual);
                UpdateCamera();
                ViewStatusText.Text = string.Format(
                    CultureInfo.InvariantCulture,
                    "{0} route(s), {1} panel box(es), {2} tray(s) drawn{3}  |  "
                    + "drag to orbit, right-drag to pan, wheel to zoom.",
                    scene.RouteCount, scene.PanelCount, scene.TrayCount,
                    scene.Capped ? ", capped at " + Scene3D.MaxBoxes.ToString()
                        + " boxes" : "");
            }
            catch (Exception error)
            {
                ViewStatusText.Text = "The 3D preview could not be drawn ("
                    + error.Message + "). The other tabs are unaffected.";
            }
        }

        private void UpdateCamera()
        {
            double azimuth = _azimuth * Math.PI / 180.0;
            double elevation = Math.Max(-89.0, Math.Min(89.0, _elevation))
                * Math.PI / 180.0;
            double x = _distance * Math.Cos(elevation) * Math.Cos(azimuth);
            double y = _distance * Math.Cos(elevation) * Math.Sin(azimuth);
            double z = _distance * Math.Sin(elevation);
            _camera.Position = new Point3D(
                _target.X + x, _target.Y + y, _target.Z + z);
            _camera.LookDirection = new Vector3D(-x, -y, -z);
            _camera.UpDirection = new Vector3D(0.0, 0.0, 1.0);
        }

        private void SetView(double azimuth, double elevation)
        {
            _azimuth = azimuth;
            _elevation = elevation;
            UpdateCamera();
        }

        private void OnViewIso(object sender, RoutedEventArgs args)
        {
            SetView(35.0, 26.0);
        }

        private void OnViewTop(object sender, RoutedEventArgs args)
        {
            SetView(90.0, 88.0);
        }

        private void OnViewFront(object sender, RoutedEventArgs args)
        {
            SetView(0.0, 3.0);
        }

        private void OnViewSide(object sender, RoutedEventArgs args)
        {
            SetView(90.0, 3.0);
        }

        private void OnViewFit(object sender, RoutedEventArgs args)
        {
            _distance = 24.0;
            _target = new Vector3D(0.0, 0.0, 0.0);
            UpdateCamera();
        }

        private void OnViewMouseDown(object sender, MouseButtonEventArgs args)
        {
            _dragMode = args.ChangedButton == MouseButton.Right
                ? "pan" : "orbit";
            _dragOrigin = args.GetPosition(ViewHost);
            ViewHost.CaptureMouse();
        }

        private void OnViewMouseUp(object sender, MouseButtonEventArgs args)
        {
            _dragMode = null;
            ViewHost.ReleaseMouseCapture();
        }

        private void OnViewMouseMove(object sender, MouseEventArgs args)
        {
            if (_dragMode == null)
            {
                return;
            }
            Point position = args.GetPosition(ViewHost);
            double deltaX = position.X - _dragOrigin.X;
            double deltaY = position.Y - _dragOrigin.Y;
            _dragOrigin = position;
            if (_dragMode == "orbit")
            {
                _azimuth = (_azimuth - deltaX * 0.4) % 360.0;
                _elevation = Math.Max(-89.0,
                    Math.Min(89.0, _elevation + deltaY * 0.4));
            }
            else
            {
                double azimuth = _azimuth * Math.PI / 180.0;
                double elevation = _elevation * Math.PI / 180.0;
                Vector3D right = new Vector3D(
                    -Math.Sin(azimuth), Math.Cos(azimuth), 0.0);
                Vector3D forward = new Vector3D(
                    -Math.Cos(elevation) * Math.Cos(azimuth),
                    -Math.Cos(elevation) * Math.Sin(azimuth),
                    -Math.Sin(elevation));
                Vector3D up = Vector3D.CrossProduct(right, forward);
                double step = _distance * 0.0016;
                _target -= right * deltaX * step;
                _target += up * deltaY * step;
            }
            UpdateCamera();
        }

        private void OnViewMouseWheel(object sender, MouseWheelEventArgs args)
        {
            double factor = args.Delta > 0 ? 0.88 : 1.0 / 0.88;
            _distance = Math.Max(1.5, Math.Min(240.0, _distance * factor));
            UpdateCamera();
        }

        // ----- export and close -------------------------------------------------
        private List<RouteRecord> Ticked()
        {
            List<RouteRecord> ticked = new List<RouteRecord>();
            for (int index = 0; index < _rows.Count; index++)
            {
                if (_rows[index].Include)
                {
                    ticked.Add(_rows[index].Record);
                }
            }
            return ticked;
        }

        private void OnCopyReport(object sender, RoutedEventArgs args)
        {
            try
            {
                List<string> lines = Export.Report(
                    _result, _options, _trays.Count, _nodeCount,
                    _fittingsBridged);
                string text = string.Join(Environment.NewLine, lines.ToArray())
                    + Environment.NewLine + Environment.NewLine
                    + Export.Tabbed(Export.Rows(Ticked(), _options));
                Clipboard.SetText(text);
                UpdateBanner("Report and " + Ticked().Count.ToString()
                    + " ticked route(s) copied - paste straight into Excel, "
                    + "the columns are tab separated.", true);
            }
            catch (Exception error)
            {
                UpdateBanner("Could not copy to the clipboard: "
                    + error.Message, false);
            }
        }

        private string WriteCsv()
        {
            List<RouteRecord> ticked = Ticked();
            if (ticked.Count == 0)
            {
                UpdateBanner("Tick at least one route to export.", false);
                return null;
            }
            string path = ExcelExport.AskForPath(this);
            if (string.IsNullOrEmpty(path))
            {
                return null;
            }
            try
            {
                ExcelExport.WriteCsv(
                    path, Export.Csv(Export.Rows(ticked, _options)));
            }
            catch (Exception error)
            {
                UpdateBanner("Could not write the file: " + error.Message,
                    false);
                return null;
            }
            Exported.Add(path);
            return path;
        }

        private void OnExportCsv(object sender, RoutedEventArgs args)
        {
            string path = WriteCsv();
            if (path != null)
            {
                UpdateBanner(Ticked().Count.ToString()
                    + " route(s) exported to " + path, true);
            }
        }

        private void OnOpenInExcel(object sender, RoutedEventArgs args)
        {
            string path = WriteCsv();
            if (path == null)
            {
                return;
            }
            string saved;
            string failure = ExcelExport.OpenInExcel(path, out saved);
            if (failure != null)
            {
                UpdateBanner("Excel could not be opened (" + failure
                    + "). The file is written and waiting at " + path, false);
                return;
            }
            UpdateBanner("Opened in Excel with the header frozen and filters "
                + "on" + (string.IsNullOrEmpty(saved)
                    ? " (still a CSV - save it as a workbook in Excel)."
                    : ", saved as " + saved + "."), true);
        }

        private void OnDraw(object sender, RoutedEventArgs args)
        {
            if (Ticked().Count == 0)
            {
                UpdateBanner("Tick at least one route to draw.", false);
                return;
            }
            Choice = ResultChoice.Draw;
            DialogResult = true;
            Close();
        }

        private void OnClose(object sender, RoutedEventArgs args)
        {
            Choice = ResultChoice.Close;
            DialogResult = false;
            Close();
        }
    }
}
