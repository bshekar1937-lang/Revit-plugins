using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Media3D;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.UI;
// Aliases: Revit and WPF share several type names (TextBox, ComboBox, Color, Grid ...).
// An alias always wins over a namespace import, so these names are never ambiguous.
using WpfTextBox = System.Windows.Controls.TextBox;
using WpfGrid = System.Windows.Controls.Grid;
using WpfButton = System.Windows.Controls.Button;
using WpfCheckBox = System.Windows.Controls.CheckBox;
using WpfColor = System.Windows.Media.Color;
using WpfHorizontalAlignment = System.Windows.HorizontalAlignment;
using WpfVerticalAlignment = System.Windows.VerticalAlignment;
using WpfMessageBox = System.Windows.MessageBox;
using Orientation = System.Windows.Controls.Orientation;

namespace MyRevitCommands
{
    [Transaction(TransactionMode.Manual)]
    public class PanelDistanceCommand : IExternalCommand
    {
        private const string ToolName = "Panel Distance";

        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            string stage = "starting";
            try
            {
                UIDocument uiDocument = commandData.Application.ActiveUIDocument;
                if (uiDocument == null)
                {
                    message = "Open a project before running " + ToolName + ".";
                    return Result.Failed;
                }
                Document document = uiDocument.Document;

                stage = "collecting electrical equipment and cable trays";
                List<Element> panels = new FilteredElementCollector(document)
                    .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                    .WhereElementIsNotElementType()
                    .ToElements()
                    .OrderBy(element => GetElementName(element), StringComparer.CurrentCultureIgnoreCase)
                    .ToList();
                List<Element> trays = new FilteredElementCollector(document)
                    .OfCategory(BuiltInCategory.OST_CableTray)
                    .WhereElementIsNotElementType()
                    .ToElements()
                    .ToList();

                if (panels.Count == 0)
                {
                    TaskDialog.Show(ToolName, "No electrical equipment was found in this model.");
                    return Result.Cancelled;
                }
                if (trays.Count == 0)
                {
                    TaskDialog.Show(ToolName, "No cable trays were found. Routes follow the modelled tray centre lines.");
                    return Result.Cancelled;
                }

                stage = "opening the setup window";
                var setup = new SetupWindow(panels, trays.Count, commandData.Application.MainWindowHandle);
                if (setup.ShowDialog() != true)
                    return Result.Cancelled;

                stage = "building the cable tray network";
                var network = new TrayNetwork(trays, setup.ToleranceMm, setup.BridgeGapMm, setup.TrayPreference);
                stage = "measuring routes";
                List<Route> routes = MeasureRoutes(document, network, setup);
                if (routes.Count == 0)
                {
                    TaskDialog.Show(ToolName, "No routes could be measured. Check that the selected panels and equipment are near connected cable trays.");
                    return Result.Succeeded;
                }

                // "Draw red detail lines after measuring" was ignored before: honour it here.
                string drawNote = null;
                bool drawn = false;
                if (setup.DrawLines)
                {
                    stage = "drawing detail lines";
                    try
                    {
                        DrawRoutes(document, routes);
                        drawn = true;
                        drawNote = "Red detail lines were drawn in the active plan view.";
                    }
                    catch (Exception exception)
                    {
                        drawNote = "Detail lines were not drawn: " + exception.Message;
                    }
                }

                stage = "showing the results window";
                var resultWindow = new ResultsWindow(document, uiDocument, routes, setup, network.Issues, network.PreviewTrays, drawn, drawNote, commandData.Application.MainWindowHandle);
                resultWindow.ShowDialog();
                return Result.Succeeded;
            }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException)
            {
                return Result.Cancelled;
            }
            catch (Exception exception)
            {
                ShowError(stage, exception);
                return Result.Cancelled; // error already shown with full details
            }
        }

        /// <summary>
        /// Shows WHERE the tool failed (stage + stack trace) and writes the same
        /// text to %TEMP%\PanelDistance_error.log so it can be sent for support.
        /// Copy the .pdb next to the .dll to get line numbers in the trace.
        /// </summary>
        private static void ShowError(string stage, Exception exception)
        {
            string details = "Stage: " + stage + Environment.NewLine
                + "Revit add-in version: " + typeof(PanelDistanceCommand).Assembly.GetName().Version + Environment.NewLine
                + Environment.NewLine + exception;
            string logPath = Path.Combine(Path.GetTempPath(), "PanelDistance_error.log");
            try { File.WriteAllText(logPath, DateTime.Now.ToString("u", CultureInfo.InvariantCulture) + Environment.NewLine + details); }
            catch (Exception) { logPath = null; }

            var dialog = new TaskDialog(ToolName)
            {
                MainIcon = TaskDialogIcon.TaskDialogIconError,
                MainInstruction = "Panel Distance stopped while " + stage + ".",
                MainContent = exception.Message
                    + (logPath == null ? string.Empty : "\n\nFull details were saved to:\n" + logPath),
                ExpandedContent = details,
                AllowCancellation = true
            };
            dialog.Show();
        }

        // ------------------------------------------------------------------
        // Measuring
        // ------------------------------------------------------------------

        private static List<Route> MeasureRoutes(Document document, TrayNetwork network, SetupWindow settings)
        {
            var routes = new List<Route>();
            List<Element> panels = settings.SelectedPanels;
            // Compare by id: two Element objects for the same element are different
            // .NET instances, so List.Contains(element) never matched before.
            var panelIds = new HashSet<long>(panels.Select(panel => IdValue(panel.Id)));
            var circuitPairs = new HashSet<string>();
            List<ElectricalSystem> systems = new FilteredElementCollector(document)
                .OfClass(typeof(ElectricalSystem))
                .Cast<ElectricalSystem>()
                .ToList();

            foreach (ElectricalSystem system in systems)
            {
                // Some circuits (spares, spaces, unassigned or corrupt systems) throw or
                // return nulls from the API. Skip and report them instead of stopping.
                Element baseEquipment;
                List<Element> members;
                string circuitNumber;
                try
                {
                    baseEquipment = system.BaseEquipment;
                    if (baseEquipment == null || baseEquipment.Id == null || !panelIds.Contains(IdValue(baseEquipment.Id)))
                        continue;
                    ElementSet set = system.Elements;
                    members = set == null ? new List<Element>() : set.Cast<Element>().Where(member => member != null).ToList();
                    circuitNumber = system.CircuitNumber ?? string.Empty;
                }
                catch (Exception exception)
                {
                    network.Issues.Add("Circuit (system ID " + SafeId(system) + ") skipped: " + exception.Message);
                    continue;
                }

                foreach (Element member in members)
                {
                    try
                    {
                        if (member.Id == baseEquipment.Id)
                            continue;
                        circuitPairs.Add(PairKey(baseEquipment.Id, member.Id));
                        bool isolator = IsolatorName(member);
                        if ((!settings.IncludeCircuits && !isolator) || (isolator && !settings.IncludeIsolators))
                            continue;
                        Route route = network.Measure(baseEquipment, member, isolator ? "Isolator" : "Circuit");
                        if (route == null)
                            continue;
                        route.Circuit = circuitNumber;
                        route.Current = ReadElectricalValue(system, settings.Current, "ApparentCurrent", "Current");
                        route.Voltage = ReadElectricalValue(system, settings.Voltage, "NominalVoltage", "Voltage");
                        route.Calculate(settings);
                        routes.Add(route);
                    }
                    catch (Exception exception)
                    {
                        network.Issues.Add("Circuit " + circuitNumber + " to " + SafeName(member) + " (ID " + SafeId(member) + ") skipped: " + exception.Message);
                    }
                }
            }

            if (settings.IncludePairs)
            {
                for (int i = 0; i < panels.Count; i++)
                {
                    for (int j = i + 1; j < panels.Count; j++)
                    {
                        try
                        {
                            if (circuitPairs.Contains(PairKey(panels[i].Id, panels[j].Id)))
                                continue;
                            Route route = network.Measure(panels[i], panels[j], "Panel pair");
                            if (route == null || (settings.MaximumPairMetres > 0 && route.LengthMetres > settings.MaximumPairMetres))
                                continue;
                            route.Calculate(settings);
                            routes.Add(route);
                        }
                        catch (Exception exception)
                        {
                            network.Issues.Add("Pair " + SafeName(panels[i]) + " / " + SafeName(panels[j]) + " skipped: " + exception.Message);
                        }
                    }
                }
            }

            return routes.OrderByDescending(route => route.DropPercent).ThenByDescending(route => route.LengthMetres).ToList();
        }

        private static double ReadElectricalValue(ElectricalSystem system, double fallback, params string[] properties)
        {
            foreach (string name in properties)
            {
                try
                {
                    PropertyInfo property = system.GetType().GetProperty(name);
                    object value = property == null ? null : property.GetValue(system, null);
                    if (!(value is double))
                        continue;
                    double number = (double)value;
                    if (number <= 0)
                        continue;
                    ForgeTypeId unit = name.IndexOf("Voltage", StringComparison.OrdinalIgnoreCase) >= 0
                        ? UnitTypeId.Volts
                        : UnitTypeId.Amperes;
                    try { return UnitUtils.ConvertFromInternalUnits(number, unit); }
                    catch (Exception) { return number; } // unit not convertible: value is already in V / A
                }
                catch (TargetInvocationException) { }
                catch (ArgumentException) { }
                catch (Autodesk.Revit.Exceptions.InvalidOperationException) { }
            }
            return fallback;
        }

        // ------------------------------------------------------------------
        // Helpers
        // ------------------------------------------------------------------

        /// <summary>
        /// ElementId.Value exists from Revit 2024; IntegerValue was removed in 2026.
        /// Define REVIT2023 (or older) in the project for older builds.
        /// </summary>
        private static long IdValue(ElementId id)
        {
#if REVIT2021 || REVIT2022 || REVIT2023
            return id.IntegerValue;
#else
            return id.Value;
#endif
        }

        private static string SafeId(Element element)
        {
            try { return element == null || element.Id == null ? "?" : IdValue(element.Id).ToString(CultureInfo.InvariantCulture); }
            catch (Exception) { return "?"; }
        }

        private static string CategoryName(Element element)
        {
            try { return element == null || element.Category == null ? string.Empty : element.Category.Name ?? string.Empty; }
            catch (Exception) { return string.Empty; }
        }

        private static string SafeName(Element element)
        {
            try { return GetElementName(element); }
            catch (Exception) { return "Element " + SafeId(element); }
        }

        private static string PairKey(ElementId first, ElementId second)
        {
            long a = IdValue(first);
            long b = IdValue(second);
            return a < b ? a + ":" + b : b + ":" + a;
        }

        private static bool IsolatorName(Element element)
        {
            string text = (GetElementName(element) + " " + ReadParameter(element, "Comments") + " " + ReadParameter(element, "JAC-EL-Rating")).ToLowerInvariant();
            return text.Contains("isolator");
        }

        private static string ReadParameter(Element element, string name)
        {
            if (element == null) return string.Empty;
            try
            {
                Parameter parameter = element.LookupParameter(name);
                if (parameter == null || !parameter.HasValue)
                    return string.Empty;
                return parameter.AsString() ?? parameter.AsValueString() ?? string.Empty;
            }
            catch (Exception)
            {
                return string.Empty;
            }
        }

        private static string GetElementName(Element element)
        {
            if (element == null)
                return string.Empty;
            foreach (string parameterName in new[] { "Panel Name", "Panel", "Mark", "Equipment Name", "Type Mark" })
            {
                string value = ReadParameter(element, parameterName);
                if (!string.IsNullOrWhiteSpace(value)) return value;
            }
            string name = null;
            try { name = element.Name; } catch (Exception) { }
            return string.IsNullOrWhiteSpace(name) ? "Element " + SafeId(element) : name;
        }

        private static List<XYZ> GetElementPoints(Element element)
        {
            var points = new List<XYZ>();
            if (element == null) return points;
            FamilyInstance instance = element as FamilyInstance;
            try
            {
                MEPModel model = instance == null ? null : instance.MEPModel;
                ConnectorManager manager = model == null ? null : model.ConnectorManager;
                if (manager != null && manager.Connectors != null)
                {
                    foreach (Connector connector in manager.Connectors)
                    {
                        try
                        {
                            if (connector != null && connector.Origin != null) points.Add(connector.Origin);
                        }
                        catch (Exception) { } // logical connectors have no origin
                    }
                }
            }
            catch (Exception) { }
            if (points.Count > 0) return points;
            LocationPoint locationPoint = element.Location as LocationPoint;
            if (locationPoint != null) return new List<XYZ> { locationPoint.Point };
            LocationCurve locationCurve = element.Location as LocationCurve;
            if (locationCurve != null && locationCurve.Curve != null) return new List<XYZ> { locationCurve.Curve.GetEndPoint(0), locationCurve.Curve.GetEndPoint(1) };
            BoundingBoxXYZ box = element.get_BoundingBox(null);
            return box == null ? points : new List<XYZ> { (box.Min + box.Max) * 0.5 };
        }

        private static void DrawRoutes(Document document, IList<Route> routes)
        {
            ViewPlan view = document.ActiveView as ViewPlan;
            if (view == null || view.IsTemplate)
                throw new InvalidOperationException("Open a floor, ceiling, or engineering plan view before drawing detail lines.");

            using (var transaction = new Transaction(document, "Draw panel distance routes"))
            {
                transaction.Start();
                Category lines = document.Settings.Categories.get_Item(BuiltInCategory.OST_Lines);
                Category styleCategory = lines.SubCategories.Cast<Category>().FirstOrDefault(category => category.Name == "Auto Panel Distance - Red");
                if (styleCategory == null) styleCategory = document.Settings.Categories.NewSubcategory(lines, "Auto Panel Distance - Red");
                styleCategory.LineColor = new Autodesk.Revit.DB.Color(220, 38, 38);
                styleCategory.SetLineWeight(5, GraphicsStyleType.Projection);
                GraphicsStyle style = styleCategory.GetGraphicsStyle(GraphicsStyleType.Projection);
                XYZ normal = view.ViewDirection.Normalize();
                XYZ origin = view.Origin;
                double shortest = document.Application.ShortCurveTolerance;
                int created = 0;
                foreach (Route route in routes)
                {
                    // Flatten every route point onto the view plane.
                    List<XYZ> points = route.Points.Select(point => point - normal.Multiply((point - origin).DotProduct(normal))).ToList();
                    for (int i = 0; i < points.Count - 1; i++)
                    {
                        if (points[i].DistanceTo(points[i + 1]) < shortest) continue;
                        DetailCurve detail = document.Create.NewDetailCurve(view, Autodesk.Revit.DB.Line.CreateBound(points[i], points[i + 1]));
                        detail.LineStyle = style;
                        created++;
                    }
                }
                if (created == 0)
                {
                    transaction.RollBack();
                    throw new InvalidOperationException("No route segments could be drawn in the active view.");
                }
                transaction.Commit();
            }
        }

        private static string Csv(string value)
        {
            return "\"" + (value ?? string.Empty).Replace("\"", "\"\"") + "\"";
        }

        private static string Number(double value, string format)
        {
            return value.ToString(format, CultureInfo.InvariantCulture);
        }

        // ------------------------------------------------------------------
        // Route (PUBLIC PROPERTIES: WPF DataGrid bindings cannot read fields,
        // which is why the Type/Circuit/Tray/Drop columns were empty before).
        // ------------------------------------------------------------------

        public sealed class Route
        {
            public Element Source { get; set; }
            public Element Destination { get; set; }
            public string Kind { get; set; }
            public string Circuit { get; set; } = string.Empty;
            public double LengthMetres { get; set; }
            public double TrayMetres { get; set; }
            public double EndMetres { get; set; }
            public double Current { get; set; }
            public double Voltage { get; set; }
            public double DropVolts { get; set; }
            public double DropPercent { get; set; }
            public double AllowanceLength { get; set; }
            public bool WithinLimit { get; set; }
            public List<XYZ> Points { get; set; } = new List<XYZ>();

            public string SourceName { get { return GetElementName(Source); } }
            public string DestinationName { get { return GetElementName(Destination); } }
            public string Status { get { return WithinLimit ? "OK" : "OVER"; } }

            internal void Calculate(SetupWindow settings)
            {
                if (Current <= 0) Current = settings.Current;
                if (Voltage <= 0) Voltage = settings.Voltage;
                AllowanceLength = LengthMetres * (1.0 + settings.Allowance / 100.0);
                DropVolts = settings.MilliVoltsPerAmpMetre * Current * AllowanceLength / 1000.0;
                DropPercent = Voltage > 0 ? DropVolts / Voltage * 100.0 : 0;
                WithinLimit = DropPercent <= settings.LimitPercent;
            }
        }

        // ------------------------------------------------------------------
        // Tray network graph + Dijkstra
        // ------------------------------------------------------------------

        private sealed class TrayNetwork
        {
            private sealed class Edge
            {
                public int From;
                public int To;
                public double Cost;
                public double Length;
                public List<XYZ> Points;
            }

            private sealed class Segment
            {
                public Curve Curve;
                public XYZ Start;
                public XYZ End;
                public int NodeStart;
                public int NodeEnd;
                public double Length;
            }

            private sealed class Attachment
            {
                public Segment Segment;
                public XYZ ElementPoint;
                public XYZ Projection;
                public double Gap;
                public double Along;
            }

            private const double FeetToMetres = 0.3048;
            private const double MmPerFoot = 304.8;
            private const double AttachSlackFeet = 6.56; // ~2 m beyond the nearest tray

            private readonly Dictionary<int, List<Edge>> _graph = new Dictionary<int, List<Edge>>();
            private readonly Dictionary<int, XYZ> _nodePoints = new Dictionary<int, XYZ>();
            private readonly List<Segment> _segments = new List<Segment>();
            private readonly double _tolerance;
            private readonly double _preference;
            private int _nextNode;

            public List<string> Issues { get; private set; }
            public List<List<XYZ>> PreviewTrays { get; private set; }

            public TrayNetwork(IEnumerable<Element> trays, double toleranceMm, double bridgeGapMm, double preference)
            {
                Issues = new List<string>();
                PreviewTrays = new List<List<XYZ>>();
                _tolerance = toleranceMm / MmPerFoot;
                _preference = Math.Max(1, preference);
                int skipped = 0;
                foreach (Element tray in trays)
                {
                    LocationCurve location = tray == null ? null : tray.Location as LocationCurve;
                    Curve curve = location == null ? null : location.Curve;
                    if (curve == null || !curve.IsBound) { skipped++; continue; }
                    XYZ start = curve.GetEndPoint(0);
                    XYZ end = curve.GetEndPoint(1);
                    int a = NodeAt(start);
                    int b = NodeAt(end);
                    List<XYZ> shape;
                    try { shape = curve.Tessellate().ToList(); }
                    catch (Exception) { shape = new List<XYZ> { start, end }; }
                    _segments.Add(new Segment { Curve = curve, Start = start, End = end, NodeStart = a, NodeEnd = b, Length = curve.Length });
                    PreviewTrays.Add(shape);
                    AddEdge(a, b, curve.Length, curve.Length, shape);
                }
                if (skipped > 0) Issues.Add(skipped + " cable tray element(s) without a centre line were ignored.");
                BridgeDisconnectedComponents(bridgeGapMm / MmPerFoot);
            }

            private int NodeAt(XYZ point)
            {
                foreach (KeyValuePair<int, XYZ> item in _nodePoints)
                    if (item.Value.DistanceTo(point) <= _tolerance) return item.Key;
                int id = _nextNode++;
                _graph[id] = new List<Edge>();
                _nodePoints[id] = point;
                return id;
            }

            private void AddEdge(int from, int to, double cost, double length, List<XYZ> points)
            {
                if (from == to) return;
                var backwardPoints = points.AsEnumerable().Reverse().ToList();
                _graph[from].Add(new Edge { From = from, To = to, Cost = cost, Length = length, Points = points });
                _graph[to].Add(new Edge { From = to, To = from, Cost = cost, Length = length, Points = backwardPoints });
            }

            private void BridgeDisconnectedComponents(double maxGap)
            {
                if (maxGap <= 0 || _nodePoints.Count < 2) return;
                Dictionary<int, int> components = Components();
                var best = new Dictionary<string, Tuple<int, int, double>>();
                foreach (KeyValuePair<int, XYZ> first in _nodePoints)
                {
                    foreach (KeyValuePair<int, XYZ> second in _nodePoints)
                    {
                        if (first.Key >= second.Key || components[first.Key] == components[second.Key]) continue;
                        double distance = first.Value.DistanceTo(second.Value);
                        if (distance > maxGap) continue;
                        string key = Math.Min(components[first.Key], components[second.Key]) + ":" + Math.Max(components[first.Key], components[second.Key]);
                        Tuple<int, int, double> previous;
                        if (!best.TryGetValue(key, out previous) || distance < previous.Item3)
                            best[key] = Tuple.Create(first.Key, second.Key, distance);
                    }
                }
                foreach (Tuple<int, int, double> bridge in best.Values)
                    AddEdge(bridge.Item1, bridge.Item2, bridge.Item3 * _preference, bridge.Item3, new List<XYZ> { _nodePoints[bridge.Item1], _nodePoints[bridge.Item2] });
                if (best.Count > 0) Issues.Add(best.Count + " cable tray gap(s) bridged, up to " + (maxGap * MmPerFoot).ToString("0", CultureInfo.CurrentCulture) + " mm.");
            }

            private Dictionary<int, int> Components()
            {
                var found = new Dictionary<int, int>();
                int component = 0;
                foreach (int start in _nodePoints.Keys)
                {
                    if (found.ContainsKey(start)) continue;
                    var pending = new Queue<int>();
                    pending.Enqueue(start);
                    found[start] = component;
                    while (pending.Count > 0)
                    {
                        int current = pending.Dequeue();
                        foreach (Edge edge in _graph[current])
                            if (!found.ContainsKey(edge.To)) { found[edge.To] = component; pending.Enqueue(edge.To); }
                    }
                    component++;
                }
                return found;
            }

            public Route Measure(Element source, Element destination, string kind)
            {
                if (source == null || destination == null || _segments.Count == 0) return null;
                List<XYZ> sourcePoints = GetElementPoints(source);
                List<XYZ> destinationPoints = GetElementPoints(destination);
                if (sourcePoints.Count == 0 || destinationPoints.Count == 0) return null;
                List<Attachment> starts = Attach(sourcePoints);
                List<Attachment> ends = Attach(destinationPoints);
                if (starts.Count == 0 || ends.Count == 0) return null;

                double bestCost = double.PositiveInfinity;
                Route bestRoute = null;
                foreach (Attachment start in starts)
                {
                    foreach (bool startAtA in new[] { true, false })
                    {
                        int startNode = startAtA ? start.Segment.NodeStart : start.Segment.NodeEnd;
                        double startAlong = startAtA ? start.Along : start.Segment.Length - start.Along;
                        Dictionary<int, double> distances;
                        Dictionary<int, Edge> previous;
                        Dijkstra(startNode, start.Gap * _preference + startAlong, out distances, out previous);

                        foreach (Attachment end in ends)
                        {
                            // Both ends on the same tray: run straight along it.
                            if (end.Segment == start.Segment)
                            {
                                double along = Math.Abs(end.Along - start.Along);
                                double directCost = start.Gap * _preference + along + end.Gap * _preference;
                                if (directCost < bestCost)
                                {
                                    var direct = new List<XYZ> { start.ElementPoint };
                                    direct.AddRange(Orthogonal(start.ElementPoint, start.Projection));
                                    direct.AddRange(Orthogonal(end.Projection, end.ElementPoint));
                                    bestCost = directCost;
                                    bestRoute = NewRoute(source, destination, kind, start.Gap, along, end.Gap, RemoveDuplicatePoints(direct));
                                }
                            }

                            foreach (bool endAtA in new[] { true, false })
                            {
                                int endNode = endAtA ? end.Segment.NodeStart : end.Segment.NodeEnd;
                                double endAlong = endAtA ? end.Along : end.Segment.Length - end.Along;
                                double reached;
                                if (!distances.TryGetValue(endNode, out reached)) continue;
                                double cost = reached + endAlong + end.Gap * _preference;
                                if (cost >= bestCost) continue;
                                List<Edge> path = Reconstruct(startNode, endNode, previous);
                                if (path == null) continue;

                                var points = new List<XYZ> { start.ElementPoint };
                                points.AddRange(Orthogonal(start.ElementPoint, start.Projection));
                                points.Add(startAtA ? start.Segment.Start : start.Segment.End);
                                foreach (Edge edge in path) points.AddRange(edge.Points.Skip(1));
                                points.Add(endAtA ? end.Segment.Start : end.Segment.End);
                                points.Add(end.Projection);
                                points.AddRange(Orthogonal(end.Projection, end.ElementPoint));

                                double trayLength = startAlong + PathLength(path) + endAlong;
                                if (start.Gap + trayLength + end.Gap <= 0) continue;
                                bestCost = cost;
                                bestRoute = NewRoute(source, destination, kind, start.Gap, trayLength, end.Gap, RemoveDuplicatePoints(points));
                            }
                        }
                    }
                }
                return bestRoute;
            }

            private static Route NewRoute(Element source, Element destination, string kind, double startGap, double trayLength, double endGap, List<XYZ> points)
            {
                return new Route
                {
                    Source = source,
                    Destination = destination,
                    Kind = kind,
                    LengthMetres = (startGap + trayLength + endGap) * FeetToMetres,
                    TrayMetres = trayLength * FeetToMetres,
                    EndMetres = (startGap + endGap) * FeetToMetres,
                    Points = points
                };
            }

            private List<Attachment> Attach(List<XYZ> points)
            {
                var candidates = new List<Attachment>();
                foreach (XYZ point in points)
                {
                    foreach (Segment segment in _segments)
                    {
                        IntersectionResult projection = segment.Curve.Project(point);
                        if (projection == null) continue;
                        double normalized;
                        try { normalized = segment.Curve.ComputeNormalizedParameter(projection.Parameter); }
                        catch (Exception) { normalized = segment.Start.DistanceTo(projection.XYZPoint) / Math.Max(segment.Length, 1e-9); }
                        normalized = Math.Min(1, Math.Max(0, normalized));
                        // Off-tray travel is measured orthogonally (X + Y + Z), like a real cable drop.
                        double gap = Math.Abs(point.X - projection.XYZPoint.X) + Math.Abs(point.Y - projection.XYZPoint.Y) + Math.Abs(point.Z - projection.XYZPoint.Z);
                        candidates.Add(new Attachment { Segment = segment, ElementPoint = point, Projection = projection.XYZPoint, Gap = gap, Along = segment.Length * normalized });
                    }
                }
                if (candidates.Count == 0) return candidates;
                double minimum = candidates.Min(candidate => candidate.Gap);
                return candidates.Where(candidate => candidate.Gap <= minimum + AttachSlackFeet).OrderBy(candidate => candidate.Gap).Take(8).ToList();
            }

            private void Dijkstra(int startNode, double initial, out Dictionary<int, double> distances, out Dictionary<int, Edge> previous)
            {
                distances = new Dictionary<int, double>();
                previous = new Dictionary<int, Edge>();
                var queue = new SortedSet<Tuple<double, int>>();
                distances[startNode] = initial;
                queue.Add(Tuple.Create(initial, startNode));
                while (queue.Count > 0)
                {
                    Tuple<double, int> item = queue.Min;
                    queue.Remove(item);
                    foreach (Edge edge in _graph[item.Item2])
                    {
                        double candidate = item.Item1 + edge.Cost;
                        double old;
                        if (distances.TryGetValue(edge.To, out old))
                        {
                            if (candidate >= old) continue;
                            queue.Remove(Tuple.Create(old, edge.To));
                        }
                        distances[edge.To] = candidate;
                        previous[edge.To] = edge;
                        queue.Add(Tuple.Create(candidate, edge.To));
                    }
                }
            }

            /// <summary>Returns the edges from start to end, or null when end was not reached.</summary>
            private static List<Edge> Reconstruct(int start, int end, Dictionary<int, Edge> previous)
            {
                var path = new List<Edge>();
                int current = end;
                int guard = previous.Count + 1;
                while (current != start)
                {
                    Edge edge;
                    if (!previous.TryGetValue(current, out edge) || guard-- <= 0) return null;
                    path.Add(edge);
                    current = edge.From;
                }
                path.Reverse();
                return path;
            }

            private static double PathLength(IEnumerable<Edge> path)
            {
                return path.Sum(edge => edge.Length);
            }

            private static List<XYZ> Orthogonal(XYZ from, XYZ to)
            {
                return new List<XYZ> { new XYZ(from.X, from.Y, to.Z), new XYZ(to.X, from.Y, to.Z), to };
            }

            private static List<XYZ> RemoveDuplicatePoints(List<XYZ> points)
            {
                var result = new List<XYZ>();
                foreach (XYZ point in points)
                    if (result.Count == 0 || result[result.Count - 1].DistanceTo(point) > 1e-6) result.Add(point);
                return result;
            }
        }

        // ------------------------------------------------------------------
        // Setup window - panel SEARCH is now built into the window itself
        // ------------------------------------------------------------------

        internal sealed class SetupWindow : Window
        {
            private sealed class PanelItem
            {
                public Element Element;
                public long Id;
                public string Display;
                public string SearchText;
                public WpfCheckBox Box; // one tick box per panel; ticking only affects this panel
                public override string ToString() { return Display; }
            }

            private readonly List<Element> _panels;
            private readonly List<PanelItem> _items;
            private readonly HashSet<long> _selectedIds;
            private readonly WpfCheckBox _pairs = Check("Measure every panel-to-panel pair");
            private readonly WpfCheckBox _circuits = Check("Measure equipment on panel circuits");
            private readonly WpfCheckBox _isolators = Check("Include isolators");
            private readonly WpfCheckBox _draw = Check("Draw red detail lines after measuring (active plan view)");
            private readonly WpfTextBox _search = new WpfTextBox { Padding = new Thickness(6), Margin = new Thickness(0, 4, 0, 6), ToolTip = "Type part of a panel name, mark, ID, category or comment" };
            // Tick-box list (not a multi-select ListBox): clicking one panel never changes the others.
            private readonly StackPanel _panelRows = new StackPanel { Margin = new Thickness(6, 4, 6, 4) };
            private readonly ScrollViewer _panelList = new ScrollViewer { Height = 200, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Margin = new Thickness(0, 0, 0, 6) };
            private int _shownCount;
            private readonly TextBlock _panelScope = new TextBlock { Foreground = Brush("#64748B"), Margin = new Thickness(0, 2, 0, 6) };
            private readonly WpfTextBox _allowance = Field("5");
            private readonly WpfTextBox _tolerance = Field("100");
            private readonly WpfTextBox _voltage = Field("400");
            private readonly WpfTextBox _current = Field("32");
            private readonly WpfTextBox _mv = Field("1.1");
            private readonly WpfTextBox _limit = Field("5");
            private readonly WpfTextBox _maxPair = Field("0");
            private readonly WpfTextBox _bridge = Field("3000");
            private readonly WpfTextBox _preference = Field("8");
            private bool _refreshing;

            public SetupWindow(List<Element> panels, int trayCount, IntPtr owner)
            {
                _panels = panels;
                _items = panels.Select(panel => new PanelItem
                {
                    Element = panel,
                    Id = IdValue(panel.Id),
                    Display = GetElementName(panel) + "   (ID " + IdValue(panel.Id) + ")",
                    SearchText = string.Join(" ", new[]
                    {
                        GetElementName(panel),
                        SafeName(panel),
                        IdValue(panel.Id).ToString(CultureInfo.InvariantCulture),
                        CategoryName(panel),
                        ReadParameter(panel, "Mark"),
                        ReadParameter(panel, "Comments")
                    })
                }).ToList();
                // Start with NO panel ticked so picking one panel gives exactly one panel.
                _selectedIds = new HashSet<long>();
                foreach (PanelItem item in _items)
                {
                    PanelItem current = item;
                    current.Box = new WpfCheckBox { Content = current.Display, IsChecked = false, Margin = new Thickness(0, 3, 0, 3) };
                    current.Box.Checked += (sender, args) => { if (!_refreshing) { _selectedIds.Add(current.Id); UpdateScopeText(); } };
                    current.Box.Unchecked += (sender, args) => { if (!_refreshing) { _selectedIds.Remove(current.Id); UpdateScopeText(); } };
                }

                Title = ToolName;
                Width = 820;
                Height = 860;
                MinWidth = 720;
                MinHeight = 600;
                WindowStartupLocation = WindowStartupLocation.CenterOwner;
                Background = Brush("#F1F4F9");
                FontFamily = new FontFamily("Segoe UI");
                new WindowInteropHelper(this).Owner = owner;

                var root = new DockPanel();
                var header = new StackPanel { Background = Brush("#1E3A8A"), Orientation = Orientation.Vertical };
                header.Children.Add(new TextBlock { Text = "PANEL DISTANCE", Foreground = Brushes.White, FontSize = 21, FontWeight = FontWeights.SemiBold, Margin = new Thickness(20, 16, 20, 2) });
                header.Children.Add(new TextBlock { Text = "Measure cable routes along modelled tray centre lines", Foreground = Brush("#DBEAFE"), Margin = new Thickness(20, 0, 20, 16) });
                DockPanel.SetDock(header, Dock.Top);
                root.Children.Add(header);

                var footer = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = WpfHorizontalAlignment.Right, Margin = new Thickness(16) };
                WpfButton cancel = MakeButton("Cancel", false);
                cancel.IsCancel = true;
                WpfButton run = MakeButton("Measure routes", true);
                run.IsDefault = true;
                run.Margin = new Thickness(8, 0, 0, 0);
                run.Click += RunClicked;
                footer.Children.Add(cancel);
                footer.Children.Add(run);
                DockPanel.SetDock(footer, Dock.Bottom);
                root.Children.Add(footer);

                var content = new StackPanel { Margin = new Thickness(20), Orientation = Orientation.Vertical };
                content.Children.Add(Card("Panels to measure", BuildPanelPicker()));
                content.Children.Add(Card("Scope", BuildScope()));
                content.Children.Add(Card("Route settings", BuildSettings()));
                content.Children.Add(Card("Voltage-drop calculation", BuildVoltage()));
                content.Children.Add(Card("Model", new TextBlock { Text = panels.Count + " electrical equipment item(s)\n" + trayCount + " cable tray(s)", TextWrapping = TextWrapping.Wrap, Foreground = Brush("#334155") }));
                root.Children.Add(new ScrollViewer { Content = content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
                Content = root;

                _search.TextChanged += (sender, args) => RefreshPanelList();
                RefreshPanelList();
                Loaded += (sender, args) => _search.Focus();
            }

            public List<Element> SelectedPanels { get; private set; }
            public bool IncludePairs { get { return _pairs.IsChecked == true; } }
            public bool IncludeCircuits { get { return _circuits.IsChecked == true; } }
            public bool IncludeIsolators { get { return _isolators.IsChecked == true; } }
            public bool DrawLines { get { return _draw.IsChecked == true; } }
            public double Allowance { get; private set; }
            public double ToleranceMm { get; private set; }
            public double Voltage { get; private set; }
            public double Current { get; private set; }
            public double MilliVoltsPerAmpMetre { get; private set; }
            public double LimitPercent { get; private set; }
            public double MaximumPairMetres { get; private set; }
            public double BridgeGapMm { get; private set; }
            public double TrayPreference { get; private set; }

            private UIElement BuildPanelPicker()
            {
                var panel = new StackPanel();
                panel.Children.Add(new TextBlock { Text = "Search panels by name, mark, ID, category or comments", Foreground = Brush("#475569") });
                panel.Children.Add(_search);
                _panelList.Content = _panelRows;
                panel.Children.Add(new Border { BorderBrush = Brush("#CBD5E1"), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(4), Margin = new Thickness(0, 0, 0, 6), Child = _panelList });

                var row = new StackPanel { Orientation = Orientation.Horizontal };
                WpfButton selectShown = MakeButton("Tick all shown", false);
                selectShown.Click += (sender, args) => SetShown(true);
                WpfButton clearShown = MakeButton("Untick all shown", false);
                clearShown.Margin = new Thickness(6, 0, 0, 0);
                clearShown.Click += (sender, args) => SetShown(false);
                WpfButton clearSearch = MakeButton("Clear search", false);
                clearSearch.Margin = new Thickness(6, 0, 0, 0);
                clearSearch.Click += (sender, args) => _search.Text = string.Empty;
                row.Children.Add(selectShown);
                row.Children.Add(clearShown);
                row.Children.Add(clearSearch);
                panel.Children.Add(row);
                panel.Children.Add(_panelScope);
                return panel;
            }

            private UIElement BuildScope()
            {
                var panel = new StackPanel();
                panel.Children.Add(_pairs);
                panel.Children.Add(_circuits);
                panel.Children.Add(_isolators);
                panel.Children.Add(_draw);
                return panel;
            }

            private UIElement BuildSettings()
            {
                var grid = new WpfGrid();
                for (int i = 0; i < 3; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                for (int i = 0; i < 2; i++) grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                AddField(grid, "Cable allowance (%)", _allowance, 0, 0);
                AddField(grid, "Tray join tolerance (mm)", _tolerance, 1, 0);
                AddField(grid, "Max panel-pair route (m, 0 = no limit)", _maxPair, 2, 0);
                AddField(grid, "Bridge tray gaps up to (mm)", _bridge, 0, 1);
                AddField(grid, "Off-tray travel preference", _preference, 1, 1);
                return grid;
            }

            private UIElement BuildVoltage()
            {
                var grid = new WpfGrid();
                for (int i = 0; i < 4; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                AddField(grid, "Nominal voltage (V)", _voltage, 0, 0);
                AddField(grid, "Default current (A)", _current, 1, 0);
                AddField(grid, "Cable mV/A/m", _mv, 2, 0);
                AddField(grid, "Voltage-drop limit (%)", _limit, 3, 0);
                return grid;
            }

            private void RefreshPanelList()
            {
                _refreshing = true;
                try
                {
                    string query = (_search.Text ?? string.Empty).Trim();
                    _panelRows.Children.Clear();
                    _shownCount = 0;
                    foreach (PanelItem item in _items)
                    {
                        if (query.Length > 0 && item.SearchText.IndexOf(query, StringComparison.CurrentCultureIgnoreCase) < 0)
                            continue;
                        item.Box.IsChecked = _selectedIds.Contains(item.Id);
                        _panelRows.Children.Add(item.Box);
                        _shownCount++;
                    }
                    if (_shownCount == 0)
                        _panelRows.Children.Add(new TextBlock { Text = "No panel matches \"" + query + "\".", Foreground = Brush("#64748B"), Margin = new Thickness(0, 3, 0, 3) });
                }
                finally
                {
                    _refreshing = false;
                }
                UpdateScopeText();
            }

            private void SetShown(bool selected)
            {
                foreach (PanelItem item in _panelRows.Children.OfType<WpfCheckBox>().Select(box => _items.First(candidate => candidate.Box == box)))
                {
                    if (selected) _selectedIds.Add(item.Id);
                    else _selectedIds.Remove(item.Id);
                }
                RefreshPanelList();
            }

            private void UpdateScopeText()
            {
                _panelScope.Text = _selectedIds.Count + " of " + _panels.Count + " panel(s) ticked, "
                    + _shownCount + " shown. Tick each panel you want to measure.";
            }

            private void RunClicked(object sender, RoutedEventArgs args)
            {
                try
                {
                    if (!IncludePairs && !IncludeCircuits && !IncludeIsolators) throw new FormatException("Choose at least one route type.");
                    List<Element> selected = _items.Where(item => _selectedIds.Contains(item.Id)).Select(item => item.Element).ToList();
                    if (selected.Count == 0) throw new FormatException("Tick at least one panel (or press \"Tick all shown\").");
                    if (IncludePairs && !IncludeCircuits && !IncludeIsolators && selected.Count < 2) throw new FormatException("Select at least two panels to measure panel-to-panel pairs.");
                    Allowance = Read(_allowance, "Cable allowance", 0, true);
                    ToleranceMm = Read(_tolerance, "Tray join tolerance", 0, false);
                    MaximumPairMetres = Read(_maxPair, "Maximum pair length", 0, true);
                    BridgeGapMm = Read(_bridge, "Tray gap limit", 0, true);
                    TrayPreference = Read(_preference, "Off-tray travel preference", 1, true);
                    Voltage = Read(_voltage, "Voltage", 0, false);
                    Current = Read(_current, "Current", 0, true);
                    MilliVoltsPerAmpMetre = Read(_mv, "Cable mV/A/m", 0, true);
                    LimitPercent = Read(_limit, "Voltage-drop limit", 0, true);
                    SelectedPanels = selected;
                    DialogResult = true; // closes the modal window
                }
                catch (FormatException exception)
                {
                    WpfMessageBox.Show(this, exception.Message, ToolName, MessageBoxButton.OK, MessageBoxImage.Warning);
                }
            }

            private static double Read(WpfTextBox field, string name, double minimum, bool allowMinimum)
            {
                double value;
                string text = (field.Text ?? string.Empty).Trim();
                bool parsed = double.TryParse(text, NumberStyles.Float, CultureInfo.CurrentCulture, out value)
                    || double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out value);
                if (!parsed || double.IsNaN(value) || double.IsInfinity(value) || (allowMinimum ? value < minimum : value <= minimum))
                {
                    field.Focus();
                    field.SelectAll();
                    throw new FormatException(name + " must be a number " + (allowMinimum ? "not less than " : "greater than ") + minimum.ToString(CultureInfo.CurrentCulture) + ".");
                }
                return value;
            }

            private static WpfCheckBox Check(string text)
            {
                return new WpfCheckBox { Content = text, IsChecked = true, Margin = new Thickness(0, 4, 0, 4) };
            }

            private static WpfTextBox Field(string text)
            {
                return new WpfTextBox { Text = text, MinWidth = 100, Padding = new Thickness(6), Margin = new Thickness(0, 4, 12, 8) };
            }

            private static void AddField(WpfGrid grid, string label, WpfTextBox field, int column, int row)
            {
                var stack = new StackPanel { Margin = new Thickness(0, 0, 8, 0) };
                stack.Children.Add(new TextBlock { Text = label, FontSize = 11, Foreground = Brush("#475569"), TextWrapping = TextWrapping.Wrap });
                stack.Children.Add(field);
                WpfGrid.SetColumn(stack, column);
                WpfGrid.SetRow(stack, row);
                grid.Children.Add(stack);
            }

            private static Border Card(string title, UIElement content)
            {
                var stack = new StackPanel();
                stack.Children.Add(new TextBlock { Text = title, FontSize = 14, FontWeight = FontWeights.SemiBold, Foreground = Brush("#0F172A"), Margin = new Thickness(0, 0, 0, 8) });
                stack.Children.Add(content);
                return new Border { Background = Brushes.White, BorderBrush = Brush("#E2E8F0"), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(14), Margin = new Thickness(0, 0, 0, 12), Child = stack };
            }
        }

        // ------------------------------------------------------------------
        // Results window
        // ------------------------------------------------------------------

        private sealed class ResultsWindow : Window
        {
            private readonly Document _document;
            private readonly UIDocument _uiDocument;
            private readonly List<Route> _routes;
            private readonly SetupWindow _settings;
            private readonly List<List<XYZ>> _trayPaths;
            private readonly DataGrid _routeGrid = new DataGrid { AutoGenerateColumns = false, IsReadOnly = true, CanUserAddRows = false, SelectionMode = DataGridSelectionMode.Single, HeadersVisibility = DataGridHeadersVisibility.Column };
            private readonly Viewport3D _preview = new Viewport3D { MinHeight = 300, ClipToBounds = true };
            private readonly WpfTextBox _filter = new WpfTextBox { MinWidth = 260, Padding = new Thickness(6), Margin = new Thickness(0, 0, 8, 8), ToolTip = "Search source, destination, type, circuit or status" };
            private readonly TextBlock _banner = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 8, 0), Padding = new Thickness(8), VerticalAlignment = WpfVerticalAlignment.Center };
            private readonly WpfButton _drawButton;
            private Point3D _sceneCenter;
            private double _sceneRadius = 10;
            private string _cameraPreset = "iso";
            private bool _drawn;

            public ResultsWindow(Document document, UIDocument uiDocument, List<Route> routes, SetupWindow settings, List<string> issues, List<List<XYZ>> trayPaths, bool alreadyDrawn, string drawNote, IntPtr owner)
            {
                _document = document;
                _uiDocument = uiDocument;
                _routes = routes;
                _settings = settings;
                _trayPaths = trayPaths;
                _drawn = alreadyDrawn;
                int overCount = routes.Count(route => !route.WithinLimit);

                Title = ToolName + " - Results";
                Width = 1120;
                Height = 760;
                MinWidth = 900;
                MinHeight = 600;
                WindowStartupLocation = WindowStartupLocation.CenterOwner;
                Background = Brush("#F1F4F9");
                FontFamily = new FontFamily("Segoe UI");
                new WindowInteropHelper(this).Owner = owner;

                AddColumn("Source", "SourceName", 1.4);
                AddColumn("Destination", "DestinationName", 1.4);
                AddColumn("Type", "Kind", 0.8);
                AddColumn("Circuit", "Circuit", 0.6);
                AddColumn("Tray (m)", "TrayMetres", 0.7, "0.00");
                AddColumn("Length + allowance (m)", "AllowanceLength", 1.0, "0.00");
                AddColumn("Drop (V)", "DropVolts", 0.7, "0.00");
                AddColumn("Drop (%)", "DropPercent", 0.7, "0.00");
                AddColumn("Status", "Status", 0.65);
                _routeGrid.ItemsSource = _routes;
                _routeGrid.SelectionChanged += (sender, args) => DrawPreview();
                _routeGrid.MouseDoubleClick += (sender, args) => SelectInModel();
                _filter.TextChanged += (sender, args) => ApplyFilter();

                var tabs = new TabControl { Margin = new Thickness(18, 12, 18, 0) };

                // 3D preview tab
                var previewPage = new DockPanel();
                var cameraButtons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = WpfHorizontalAlignment.Right, Margin = new Thickness(0, 0, 0, 8) };
                foreach (string preset in new[] { "Iso", "Top", "Front", "Side", "Fit" })
                {
                    string presetName = preset.ToLowerInvariant();
                    WpfButton cameraButton = MakeButton(preset, false);
                    cameraButton.Height = 26;
                    cameraButton.MinWidth = 55;
                    cameraButton.Margin = new Thickness(4, 0, 0, 0);
                    cameraButton.Click += (sender, args) => SetCamera(presetName == "fit" ? _cameraPreset : presetName);
                    cameraButtons.Children.Add(cameraButton);
                }
                DockPanel.SetDock(cameraButtons, Dock.Top);
                previewPage.Children.Add(cameraButtons);
                previewPage.Children.Add(new Border { Background = Brush("#0F172A"), CornerRadius = new CornerRadius(8), Child = _preview });
                tabs.Items.Add(new TabItem { Header = "3D preview", Content = previewPage });

                // Routes tab with search
                var routePage = new DockPanel();
                var top = new StackPanel { Orientation = Orientation.Horizontal };
                top.Children.Add(new TextBlock { Text = "Search routes", VerticalAlignment = WpfVerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 8) });
                top.Children.Add(_filter);
                top.Children.Add(new TextBlock { Text = "Double-click a row to select it in the model.", Foreground = Brush("#64748B"), VerticalAlignment = WpfVerticalAlignment.Center, Margin = new Thickness(4, 0, 0, 8) });
                DockPanel.SetDock(top, Dock.Top);
                routePage.Children.Add(top);
                routePage.Children.Add(_routeGrid);
                tabs.Items.Add(new TabItem { Header = "Routes (" + routes.Count + ")", Content = routePage });
                tabs.Items.Add(new TabItem { Header = "Panels", Content = BuildPanelSummary(routes) });
                tabs.Items.Add(new TabItem { Header = "Overview and issues", Content = BuildIssues(routes, issues, drawNote) });

                var root = new DockPanel();
                var heading = new StackPanel { Background = Brush("#1E3A8A") };
                heading.Children.Add(new TextBlock { Text = "PANEL DISTANCE", FontSize = 20, FontWeight = FontWeights.SemiBold, Foreground = Brushes.White, Margin = new Thickness(18, 12, 18, 0) });
                heading.Children.Add(new TextBlock
                {
                    Text = routes.Count + " route(s) from " + settings.SelectedPanels.Count + " panel(s) - " + overCount + " over the "
                        + settings.LimitPercent.ToString("0.##", CultureInfo.CurrentCulture) + "% limit."
                        + (alreadyDrawn ? " Detail lines have been drawn." : " Nothing is drawn until you ask."),
                    Foreground = Brush("#DBEAFE"),
                    Margin = new Thickness(18, 0, 18, 12)
                });
                DockPanel.SetDock(heading, Dock.Top);
                root.Children.Add(heading);

                var footer = new DockPanel { Margin = new Thickness(18, 12, 18, 12) };
                var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = WpfHorizontalAlignment.Right };
                WpfButton csv = MakeButton("Export CSV", false);
                csv.Click += ExportClicked;
                WpfButton close = MakeButton("Close", false);
                close.Margin = new Thickness(6, 0, 0, 0);
                close.IsCancel = true;
                close.Click += (sender, args) => Close();
                _drawButton = MakeButton(alreadyDrawn ? "Lines drawn" : "Draw detail lines", true);
                _drawButton.Margin = new Thickness(6, 0, 0, 0);
                _drawButton.IsEnabled = !alreadyDrawn;
                _drawButton.Click += DrawClicked;
                buttons.Children.Add(csv);
                buttons.Children.Add(close);
                buttons.Children.Add(_drawButton);
                DockPanel.SetDock(buttons, Dock.Right);
                footer.Children.Add(buttons);
                footer.Children.Add(_banner);
                DockPanel.SetDock(footer, Dock.Bottom);
                root.Children.Add(footer);
                root.Children.Add(tabs);
                Content = root;

                _banner.Text = overCount == 0
                    ? "All measured routes are within the voltage-drop limit."
                    : overCount + " route(s) exceed the voltage-drop limit.";
                _banner.Background = Brush(overCount == 0 ? "#F0FDF4" : "#FEF2F2");
                Loaded += (sender, args) => DrawPreview();
            }

            private void AddColumn(string heading, string property, double width, string format = null)
            {
                var binding = new System.Windows.Data.Binding(property);
                if (format != null) binding.StringFormat = format;
                _routeGrid.Columns.Add(new DataGridTextColumn { Header = heading, Binding = binding, Width = new DataGridLength(width, DataGridLengthUnitType.Star) });
            }

            private void ApplyFilter()
            {
                string search = (_filter.Text ?? string.Empty).Trim();
                _routeGrid.ItemsSource = search.Length == 0
                    ? _routes
                    : _routes.Where(route => (route.SourceName + " " + route.DestinationName + " " + route.Kind + " " + route.Circuit + " " + route.Status)
                        .IndexOf(search, StringComparison.CurrentCultureIgnoreCase) >= 0).ToList();
            }

            private void SelectInModel()
            {
                Route route = _routeGrid.SelectedItem as Route;
                if (route == null) return;
                try
                {
                    _uiDocument.Selection.SetElementIds(new List<ElementId> { route.Source.Id, route.Destination.Id });
                    _uiDocument.RefreshActiveView();
                }
                catch (Exception exception)
                {
                    WpfMessageBox.Show(this, exception.Message, ToolName);
                }
            }

            private UIElement BuildPanelSummary(List<Route> routes)
            {
                var grid = new DataGrid { AutoGenerateColumns = true, IsReadOnly = true, CanUserAddRows = false, Margin = new Thickness(4) };
                grid.ItemsSource = _settings.SelectedPanels.Select(panel =>
                {
                    List<Route> touching = routes.Where(route => route.Source.Id == panel.Id || route.Destination.Id == panel.Id).ToList();
                    return new PanelSummaryRow
                    {
                        Panel = GetElementName(panel),
                        Routes = touching.Count,
                        LongestRouteMetres = Math.Round(touching.Select(route => route.AllowanceLength).DefaultIfEmpty(0).Max(), 2),
                        WorstDropPercent = Math.Round(touching.Select(route => route.DropPercent).DefaultIfEmpty(0).Max(), 2)
                    };
                }).ToList();
                return grid;
            }

            private static UIElement BuildIssues(List<Route> routes, List<string> networkIssues, string drawNote)
            {
                var text = new StringBuilder();
                text.AppendLine(routes.Count + " route(s) measured.");
                text.AppendLine("Longest route: " + routes.Max(route => route.AllowanceLength).ToString("0.00", CultureInfo.CurrentCulture) + " m including allowance.");
                text.AppendLine("Worst voltage drop: " + routes.Max(route => route.DropPercent).ToString("0.00", CultureInfo.CurrentCulture) + "%.");
                if (!string.IsNullOrEmpty(drawNote))
                {
                    text.AppendLine();
                    text.AppendLine(drawNote);
                }
                text.AppendLine();
                text.AppendLine("Tray network notes");
                if (networkIssues.Count == 0) text.AppendLine("No disconnected tray gaps required bridging.");
                foreach (string issue in networkIssues) text.AppendLine("- " + issue);
                return new WpfTextBox { Text = text.ToString(), IsReadOnly = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(14), FontFamily = new FontFamily("Consolas"), Margin = new Thickness(8) };
            }

            private void DrawPreview()
            {
                _preview.Children.Clear();
                if (_routes.Count == 0) return;
                Route selected = _routeGrid.SelectedItem as Route;
                List<Route> shown = selected == null
                    ? ((_routeGrid.ItemsSource as IEnumerable<Route>) ?? _routes).ToList()
                    : new List<Route> { selected };
                List<XYZ> points = _trayPaths.SelectMany(path => path).Concat(shown.SelectMany(route => route.Points)).ToList();
                if (points.Count < 2) return;
                double minX = points.Min(point => point.X), maxX = points.Max(point => point.X);
                double minY = points.Min(point => point.Y), maxY = points.Max(point => point.Y);
                double minZ = points.Min(point => point.Z), maxZ = points.Max(point => point.Z);
                _sceneCenter = new Point3D((minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2);
                _sceneRadius = Math.Max(1, Math.Sqrt(Math.Pow(maxX - minX, 2) + Math.Pow(maxY - minY, 2) + Math.Pow(maxZ - minZ, 2)) / 2);

                var scene = new Model3DGroup();
                scene.Children.Add(new AmbientLight(WpfColor.FromRgb(180, 190, 210)));
                scene.Children.Add(new DirectionalLight(Colors.White, new Vector3D(-1, -2, -3)));
                WpfColor trayColor = WpfColor.FromArgb(110, 148, 163, 184);
                double trayRadius = Math.Max(_sceneRadius * 0.0015, 0.006);
                foreach (List<XYZ> tray in _trayPaths)
                {
                    for (int i = 0; i < tray.Count - 1; i++)
                    {
                        GeometryModel3D segment = CreateSegment(tray[i], tray[i + 1], trayRadius, trayColor);
                        if (segment != null) scene.Children.Add(segment);
                    }
                }
                double routeRadius = Math.Max(_sceneRadius * 0.003, 0.01);
                foreach (Route route in shown)
                {
                    WpfColor color = route.WithinLimit ? WpfColor.FromRgb(34, 211, 238) : WpfColor.FromRgb(239, 68, 68);
                    for (int i = 0; i < route.Points.Count - 1; i++)
                    {
                        GeometryModel3D segment = CreateSegment(route.Points[i], route.Points[i + 1], routeRadius, color);
                        if (segment != null) scene.Children.Add(segment);
                    }
                }
                _preview.Children.Add(new ModelVisual3D { Content = scene });
                SetCamera(_cameraPreset);
            }

            private static GeometryModel3D CreateSegment(XYZ start, XYZ end, double radius, WpfColor color)
            {
                var first = new Point3D(start.X, start.Y, start.Z);
                var last = new Point3D(end.X, end.Y, end.Z);
                Vector3D direction = last - first;
                if (direction.Length < 1e-8) return null;
                direction.Normalize();
                Vector3D reference = Math.Abs(direction.Z) < 0.9 ? new Vector3D(0, 0, 1) : new Vector3D(0, 1, 0);
                Vector3D axisA = Vector3D.CrossProduct(direction, reference);
                axisA.Normalize();
                axisA *= radius;
                Vector3D axisB = Vector3D.CrossProduct(direction, axisA);
                axisB.Normalize();
                axisB *= radius;
                var mesh = new MeshGeometry3D();
                const int sides = 8;
                for (int i = 0; i < sides; i++)
                {
                    double angle = 2 * Math.PI * i / sides;
                    Vector3D offset = axisA * Math.Cos(angle) + axisB * Math.Sin(angle);
                    mesh.Positions.Add(first + offset);
                    mesh.Positions.Add(last + offset);
                }
                for (int i = 0; i < sides; i++)
                {
                    int next = (i + 1) % sides;
                    int a = i * 2, b = a + 1, c = next * 2, d = c + 1;
                    mesh.TriangleIndices.Add(a); mesh.TriangleIndices.Add(c); mesh.TriangleIndices.Add(b);
                    mesh.TriangleIndices.Add(b); mesh.TriangleIndices.Add(c); mesh.TriangleIndices.Add(d);
                }
                var material = new DiffuseMaterial(new SolidColorBrush(color));
                return new GeometryModel3D(mesh, material) { BackMaterial = material };
            }

            private void SetCamera(string preset)
            {
                _cameraPreset = preset;
                Vector3D direction;
                Vector3D up;
                switch (preset)
                {
                    case "top": direction = new Vector3D(0, 0, 1); up = new Vector3D(0, 1, 0); break;
                    case "front": direction = new Vector3D(0, -1, 0); up = new Vector3D(0, 0, 1); break;
                    case "side": direction = new Vector3D(-1, 0, 0); up = new Vector3D(0, 0, 1); break;
                    default: direction = new Vector3D(1, -1, 1); up = new Vector3D(0, 0, 1); break;
                }
                direction.Normalize();
                double distance = Math.Max(10, _sceneRadius / Math.Tan(Math.PI / 8) * 1.5);
                Point3D position = _sceneCenter + direction * distance;
                _preview.Camera = new PerspectiveCamera(position, _sceneCenter - position, up, 45)
                {
                    NearPlaneDistance = 0.1,
                    FarPlaneDistance = distance * 4
                };
            }

            private void DrawClicked(object sender, RoutedEventArgs args)
            {
                if (_drawn) return;
                try
                {
                    DrawRoutes(_document, _routes);
                    _drawn = true;
                    _drawButton.IsEnabled = false;
                    _drawButton.Content = "Lines drawn";
                    WpfMessageBox.Show(this, "Route detail lines were drawn in the active plan view.", ToolName);
                }
                catch (Exception exception)
                {
                    WpfMessageBox.Show(this, exception.Message, ToolName, MessageBoxButton.OK, MessageBoxImage.Warning);
                }
            }

            private void ExportClicked(object sender, RoutedEventArgs args)
            {
                var dialog = new Microsoft.Win32.SaveFileDialog { Title = "Export panel distances", Filter = "CSV files (*.csv)|*.csv", FileName = "PanelDistance.csv", AddExtension = true };
                if (dialog.ShowDialog(this) != true) return;
                try
                {
                    using (var writer = new StreamWriter(dialog.FileName, false, new UTF8Encoding(true)))
                    {
                        writer.WriteLine("Source,Destination,Type,Circuit,Tray length (m),Route length (m),With allowance (m),Current (A),Voltage (V),Drop (V),Drop (%),Status");
                        foreach (Route route in _routes)
                        {
                            writer.WriteLine(string.Join(",", new[]
                            {
                                Csv(route.SourceName), Csv(route.DestinationName), Csv(route.Kind), Csv(route.Circuit),
                                Number(route.TrayMetres, "0.000"), Number(route.LengthMetres, "0.000"), Number(route.AllowanceLength, "0.000"),
                                Number(route.Current, "0.0"), Number(route.Voltage, "0.0"),
                                Number(route.DropVolts, "0.000"), Number(route.DropPercent, "0.000"), Csv(route.Status)
                            }));
                        }
                    }
                    WpfMessageBox.Show(this, "Exported " + _routes.Count + " route(s) to:\n" + dialog.FileName, ToolName);
                }
                catch (Exception exception) when (exception is IOException || exception is UnauthorizedAccessException)
                {
                    WpfMessageBox.Show(this, "Could not write the CSV file:\n" + exception.Message, ToolName, MessageBoxButton.OK, MessageBoxImage.Warning);
                }
            }
        }

        /// <summary>Row type for the Panels tab (public so WPF can bind to it).</summary>
        public sealed class PanelSummaryRow
        {
            public string Panel { get; set; }
            public int Routes { get; set; }
            public double LongestRouteMetres { get; set; }
            public double WorstDropPercent { get; set; }
        }

        // ------------------------------------------------------------------
        // Shared UI helpers
        // ------------------------------------------------------------------

        private static WpfButton MakeButton(string text, bool primary)
        {
            return new WpfButton
            {
                Content = text,
                MinWidth = primary ? 130 : 90,
                Height = 34,
                Padding = new Thickness(12, 4, 12, 4),
                Background = primary ? Brush("#2563EB") : Brushes.White,
                Foreground = primary ? Brushes.White : Brush("#334155"),
                BorderBrush = primary ? Brush("#2563EB") : Brush("#CBD5E1"),
                BorderThickness = new Thickness(1),
                FontWeight = FontWeights.SemiBold,
                Cursor = System.Windows.Input.Cursors.Hand
            };
        }

        private static SolidColorBrush Brush(string hex)
        {
            return new SolidColorBrush((WpfColor)ColorConverter.ConvertFromString(hex));
        }
    }
}
