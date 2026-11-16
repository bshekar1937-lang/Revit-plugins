using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using WpfTextBox = System.Windows.Controls.TextBox;
using WpfGrid = System.Windows.Controls.Grid;
using WpfButton = System.Windows.Controls.Button;
using WpfCheckBox = System.Windows.Controls.CheckBox;
using WpfComboBox = System.Windows.Controls.ComboBox;
using WpfRadioButton = System.Windows.Controls.RadioButton;
using WpfColor = System.Windows.Media.Color;
using WpfHorizontalAlignment = System.Windows.HorizontalAlignment;
using WpfVerticalAlignment = System.Windows.VerticalAlignment;
using WpfMessageBox = System.Windows.MessageBox;
using Orientation = System.Windows.Controls.Orientation;
using Transform = Autodesk.Revit.DB.Transform;

namespace MyRevitCommands
{
    /// <summary>
    /// LIGHT PLACEMENT (2D)
    /// Places a 2D family you choose at every lighting fixture of a linked model.
    ///  - Each linked fixture type is matched to a type of the chosen 2D family with
    ///    the same name (types you already created are reused).
    ///  - Linked types with no match get a NEW type (duplicated from a base type).
    ///  - You can review / change every match before anything is placed.
    ///  - Symbols are placed at the centre of each linked fixture (or its insertion
    ///    point) and rotated to match it.
    /// Works with model families (e.g. a 2D lighting-fixture family, placed at the
    /// fixture height) and view-specific families (detail items / generic
    /// annotations, placed in the active plan view).
    /// Everything is one transaction: a single Ctrl+Z removes it.
    /// </summary>
    [Transaction(TransactionMode.Manual)]
    public class LightPlacementCommand : IExternalCommand
    {
        internal const string ToolName = "Light Placement 2D";
        private const double DuplicateToleranceFeet = 50.0 / 304.8; // 50 mm

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
                View activeView = document.ActiveView;

                stage = "reading linked models";
                List<LinkInfo> links = new FilteredElementCollector(document)
                    .OfClass(typeof(RevitLinkInstance))
                    .Cast<RevitLinkInstance>()
                    .Select(LinkInfo.TryCreate)
                    .Where(info => info != null)
                    .OrderBy(info => info.Label, StringComparer.CurrentCultureIgnoreCase)
                    .ToList();
                if (links.Count == 0)
                {
                    TaskDialog.Show(ToolName, "No loaded Revit links were found. Load the linked model first (Manage > Manage Links).");
                    return Result.Cancelled;
                }

                stage = "reading 2D families";
                List<TargetFamily> families = TargetFamily.Collect(document);
                if (families.Count == 0)
                {
                    TaskDialog.Show(ToolName, "No Lighting Fixture, Detail Item or Generic Annotation families are loaded in this model.\n\nLoad your 2D family first (Insert > Load Family).");
                    return Result.Cancelled;
                }

                stage = "opening the setup window";
                var window = new SetupWindow(links, families, activeView, commandData.Application.MainWindowHandle);
                if (window.ShowDialog() != true)
                    return Result.Cancelled;

                stage = "placing 2D symbols";
                PlacementReport report = Place(document, activeView, window);

                stage = "showing the summary";
                ShowReport(report);
                return Result.Succeeded;
            }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException)
            {
                return Result.Cancelled;
            }
            catch (Exception exception)
            {
                var dialog = new TaskDialog(ToolName)
                {
                    MainIcon = TaskDialogIcon.TaskDialogIconError,
                    MainInstruction = ToolName + " stopped while " + stage + ".",
                    MainContent = exception.Message,
                    ExpandedContent = exception.ToString()
                };
                dialog.Show();
                return Result.Cancelled;
            }
        }

        // ==================================================================
        // Placement
        // ==================================================================

        private static PlacementReport Place(Document document, View activeView, SetupWindow settings)
        {
            var report = new PlacementReport();
            TargetFamily family = settings.SelectedFamily;
            LinkInfo link = settings.SelectedLink;
            Transform transform = link.Transform;
            bool viewBased = family.IsViewBased;

            List<Level> levels = new FilteredElementCollector(document)
                .OfClass(typeof(Level))
                .Cast<Level>()
                .OrderBy(level => level.Elevation)
                .ToList();
            Level viewLevel = viewBased && activeView is ViewPlan ? ((ViewPlan)activeView).GenLevel : null;

            using (var transaction = new Transaction(document, "Place 2D lighting symbols from link"))
            {
                FailureHandlingOptions options = transaction.GetFailureHandlingOptions();
                options.SetFailuresPreprocessor(new WarningSwallower());
                transaction.SetFailureHandlingOptions(options);
                transaction.Start();

                // 1. Resolve every mapping to a real FamilySymbol (create new types).
                var symbolFor = new Dictionary<LinkedTypeGroup, FamilySymbol>();
                HashSet<string> usedNames = new HashSet<string>(family.Symbols.Select(symbol => symbol.Name), StringComparer.CurrentCultureIgnoreCase);
                foreach (LinkedTypeGroup group in settings.Groups)
                {
                    TargetChoice choice = group.SelectedChoice;
                    if (choice == null || choice.Kind == ChoiceKind.Skip)
                    {
                        report.SkippedByChoice += group.Instances.Count;
                        continue;
                    }
                    if (choice.Kind == ChoiceKind.Existing)
                    {
                        symbolFor[group] = choice.Symbol;
                        continue;
                    }
                    try
                    {
                        string name = UniqueName(choice.NewName, usedNames);
                        var created = settings.BaseSymbol.Duplicate(name) as FamilySymbol;
                        if (created == null) throw new InvalidOperationException("Revit did not return a family type.");
                        usedNames.Add(name);
                        symbolFor[group] = created;
                        report.CreatedTypes.Add(name);
                    }
                    catch (Exception exception)
                    {
                        report.Failed.Add("Type \"" + choice.NewName + "\" could not be created: " + exception.Message);
                        report.SkippedByChoice += group.Instances.Count;
                    }
                }

                foreach (FamilySymbol symbol in symbolFor.Values.Distinct())
                    if (!symbol.IsActive) symbol.Activate();
                document.Regenerate();

                // 2. Existing symbols of this family, for duplicate checks.
                var existing = new List<Tuple<long, XYZ>>();
                if (settings.SkipDuplicates)
                {
                    FilteredElementCollector collector = viewBased
                        ? new FilteredElementCollector(document, activeView.Id)
                        : new FilteredElementCollector(document);
                    foreach (FamilyInstance instance in collector.OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>())
                    {
                        if (instance.Symbol == null || instance.Symbol.Family == null || instance.Symbol.Family.Id != family.Family.Id) continue;
                        LocationPoint location = instance.Location as LocationPoint;
                        if (location != null) existing.Add(Tuple.Create(IdValue(instance.Symbol.Id), location.Point));
                    }
                }

                // 3. Place one symbol per linked fixture.
                foreach (KeyValuePair<LinkedTypeGroup, FamilySymbol> pair in symbolFor)
                {
                    FamilySymbol symbol = pair.Value;
                    long symbolId = IdValue(symbol.Id);
                    foreach (FamilyInstance fixture in pair.Key.Instances)
                    {
                        string reference = pair.Key.Label + " (linked ID " + IdValue(fixture.Id) + ")";
                        XYZ point;
                        double rotation;
                        if (!TryGetPlacement(fixture, transform, settings.UseCentre, out point, out rotation))
                        {
                            report.Failed.Add(reference + ": no location.");
                            continue;
                        }

                        if (viewBased && settings.OnlyActiveViewLevel && viewLevel != null)
                        {
                            Level fixtureLevel = LevelFor(point.Z, levels);
                            if (fixtureLevel == null || fixtureLevel.Id != viewLevel.Id)
                            {
                                report.OutsideViewLevel++;
                                continue;
                            }
                        }

                        if (settings.SkipDuplicates && existing.Any(item => item.Item1 == symbolId && Near(item.Item2, point, viewBased)))
                        {
                            report.Duplicates++;
                            continue;
                        }

                        using (var sub = new SubTransaction(document))
                        {
                            sub.Start();
                            try
                            {
                                FamilyInstance placed = viewBased
                                    ? PlaceInView(document, activeView, symbol, point)
                                    : PlaceInModel(document, link, fixture, symbol, point, levels);
                                LocationPoint placedLocation = placed.Location as LocationPoint;
                                if (settings.Rotate && Math.Abs(rotation) > 1e-6 && placedLocation != null && !IsFaceHosted(placed))
                                {
                                    XYZ origin = placedLocation.Point;
                                    ElementTransformUtils.RotateElement(document, placed.Id, Line.CreateBound(origin, origin + XYZ.BasisZ), rotation);
                                }
                                if (settings.WriteComments)
                                    WriteComments(placed, link.Title + " | ID " + IdValue(fixture.Id) + " | " + pair.Key.Label);
                                sub.Commit();
                                existing.Add(Tuple.Create(symbolId, point));
                                report.Placed++;
                                int count;
                                report.PlacedPerType.TryGetValue(symbol.Name, out count);
                                report.PlacedPerType[symbol.Name] = count + 1;
                            }
                            catch (Exception exception)
                            {
                                sub.RollBack();
                                report.Failed.Add(reference + ": " + exception.Message);
                            }
                        }
                    }
                }

                if (report.Placed == 0 && report.CreatedTypes.Count == 0)
                    transaction.RollBack();
                else
                    transaction.Commit();
            }
            return report;
        }

        /// <summary>Host-model point (centre or insertion point) and rotation of a linked fixture.</summary>
        private static bool TryGetPlacement(FamilyInstance fixture, Transform transform, bool useCentre, out XYZ point, out double rotation)
        {
            point = null;
            rotation = 0;
            LocationPoint location = fixture.Location as LocationPoint;
            XYZ insertion = location == null ? null : location.Point;
            XYZ source = insertion;
            if (useCentre || source == null)
            {
                BoundingBoxXYZ box = fixture.get_BoundingBox(null);
                if (box != null)
                {
                    XYZ centre = (box.Min + box.Max) * 0.5;
                    // Keep the fixture's own height (Z) so the symbol sits at the same
                    // elevation; only X/Y move to the geometric centre.
                    source = insertion == null ? centre : new XYZ(centre.X, centre.Y, insertion.Z);
                }
            }
            if (source == null) return false;
            point = transform.OfPoint(source);

            XYZ hand = null;
            try { hand = fixture.HandOrientation; } catch (Exception) { }
            if (hand != null && hand.GetLength() > 1e-9)
            {
                XYZ hostHand = transform.OfVector(hand);
                rotation = Math.Atan2(hostHand.Y, hostHand.X);
            }
            return true;
        }

        private static FamilyInstance PlaceInView(Document document, View view, FamilySymbol symbol, XYZ point)
        {
            return document.Create.NewFamilyInstance(new XYZ(point.X, point.Y, 0), symbol, view);
        }

        private static FamilyInstance PlaceInModel(Document document, LinkInfo link, FamilyInstance fixture, FamilySymbol symbol, XYZ point, List<Level> levels)
        {
            var errors = new List<string>();
            Level level = LevelFor(point.Z, levels);

            // a) Unhosted / level-based / work-plane-based families.
            if (level != null && symbol.Family.FamilyPlacementType != FamilyPlacementType.OneLevelBasedHosted)
            {
                try
                {
                    FamilyInstance instance = document.Create.NewFamilyInstance(point, symbol, level, Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
                    SetOffset(instance, point.Z - level.Elevation);
                    return instance;
                }
                catch (Exception exception) { errors.Add(exception.Message); }
            }

            // b) Face-based families: host on the same (linked) face as the linked fixture.
            try
            {
                Reference hostFace = fixture.HostFace;
                if (hostFace != null)
                {
                    Reference linkFace = hostFace.CreateLinkReference(link.Instance);
                    XYZ direction = link.Transform.OfVector(fixture.HandOrientation);
                    if (direction.GetLength() > 1e-9)
                        return document.Create.NewFamilyInstance(linkFace, point, direction.Normalize(), symbol);
                }
            }
            catch (Exception exception) { errors.Add(exception.Message); }

            // c) Last resort: free placement.
            try
            {
                return document.Create.NewFamilyInstance(point, symbol, Autodesk.Revit.DB.Structure.StructuralType.NonStructural);
            }
            catch (Exception exception) { errors.Add(exception.Message); }

            throw new InvalidOperationException("could not be placed (" + string.Join(" / ", errors.Distinct()) + "). Use a non-hosted 2D family.");
        }

        private static bool IsFaceHosted(FamilyInstance instance)
        {
            try { return instance.HostFace != null; }
            catch (Exception) { return false; }
        }

        private static void SetOffset(FamilyInstance instance, double offset)
        {
            foreach (BuiltInParameter id in new[] { BuiltInParameter.INSTANCE_ELEVATION_PARAM, BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM })
            {
                Parameter parameter = instance.get_Parameter(id);
                if (parameter != null && !parameter.IsReadOnly && parameter.StorageType == StorageType.Double)
                {
                    parameter.Set(offset);
                    return;
                }
            }
        }

        private static void WriteComments(Element element, string text)
        {
            Parameter comments = element.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
            if (comments != null && !comments.IsReadOnly) comments.Set(text);
        }

        /// <summary>Highest level at or below z (fixture's own floor); else the lowest level.</summary>
        private static Level LevelFor(double z, List<Level> levels)
        {
            if (levels.Count == 0) return null;
            Level below = levels.LastOrDefault(level => level.Elevation <= z + 1e-6);
            return below ?? levels[0];
        }

        private static bool Near(XYZ a, XYZ b, bool planOnly)
        {
            double dz = planOnly ? 0 : a.Z - b.Z;
            double dx = a.X - b.X, dy = a.Y - b.Y;
            return Math.Sqrt(dx * dx + dy * dy + dz * dz) < DuplicateToleranceFeet;
        }

        private static string UniqueName(string wanted, HashSet<string> used)
        {
            string name = wanted;
            int suffix = 2;
            while (used.Contains(name)) name = wanted + " (" + suffix++ + ")";
            return name;
        }

        /// <summary>Removes characters Revit does not allow in type names.</summary>
        internal static string CleanName(string text)
        {
            const string invalid = "\\:{}[]|;<>?`~";
            var builder = new StringBuilder();
            foreach (char c in text ?? string.Empty) builder.Append(invalid.IndexOf(c) >= 0 ? '-' : c);
            string result = builder.ToString().Trim();
            return result.Length == 0 ? "Type" : result;
        }

        internal static long IdValue(ElementId id)
        {
#if REVIT2021 || REVIT2022 || REVIT2023
            return id.IntegerValue;
#else
            return id.Value;
#endif
        }

        private static void ShowReport(PlacementReport report)
        {
            var summary = new StringBuilder();
            summary.AppendLine(report.Placed + " symbol(s) placed.");
            summary.AppendLine(report.CreatedTypes.Count + " new type(s) created.");
            if (report.Duplicates > 0) summary.AppendLine(report.Duplicates + " skipped: a symbol already exists at that position.");
            if (report.OutsideViewLevel > 0) summary.AppendLine(report.OutsideViewLevel + " skipped: fixture is not on the active view's level.");
            if (report.SkippedByChoice > 0) summary.AppendLine(report.SkippedByChoice + " skipped: type set to (Skip).");
            if (report.Failed.Count > 0) summary.AppendLine(report.Failed.Count + " could not be placed - see details.");

            var details = new StringBuilder();
            if (report.PlacedPerType.Count > 0)
            {
                details.AppendLine("Placed per type:");
                foreach (KeyValuePair<string, int> item in report.PlacedPerType.OrderBy(item => item.Key))
                    details.AppendLine("  " + item.Key + ": " + item.Value);
            }
            if (report.CreatedTypes.Count > 0)
            {
                details.AppendLine();
                details.AppendLine("New types: " + string.Join(", ", report.CreatedTypes));
            }
            if (report.Failed.Count > 0)
            {
                details.AppendLine();
                details.AppendLine("Problems:");
                foreach (string line in report.Failed.Take(60)) details.AppendLine("  " + line);
                if (report.Failed.Count > 60) details.AppendLine("  ... and " + (report.Failed.Count - 60) + " more.");
            }

            var dialog = new TaskDialog(ToolName)
            {
                MainInstruction = report.Placed > 0 ? "2D lighting symbols placed" : "Nothing was placed",
                MainContent = summary.ToString().TrimEnd() + "\n\nOne Ctrl+Z undoes everything.",
                ExpandedContent = details.ToString().TrimEnd(),
                MainIcon = report.Failed.Count > 0 ? TaskDialogIcon.TaskDialogIconWarning : TaskDialogIcon.TaskDialogIconInformation
            };
            dialog.Show();
        }

        // ==================================================================
        // Data
        // ==================================================================

        private sealed class WarningSwallower : IFailuresPreprocessor
        {
            public FailureProcessingResult PreprocessFailures(FailuresAccessor failuresAccessor)
            {
                foreach (FailureMessageAccessor failure in failuresAccessor.GetFailureMessages())
                    if (failure.GetSeverity() == FailureSeverity.Warning) failuresAccessor.DeleteWarning(failure);
                return FailureProcessingResult.Continue;
            }
        }

        private sealed class PlacementReport
        {
            public int Placed;
            public int Duplicates;
            public int OutsideViewLevel;
            public int SkippedByChoice;
            public readonly List<string> CreatedTypes = new List<string>();
            public readonly List<string> Failed = new List<string>();
            public readonly Dictionary<string, int> PlacedPerType = new Dictionary<string, int>(StringComparer.CurrentCultureIgnoreCase);
        }

        internal sealed class LinkInfo
        {
            public RevitLinkInstance Instance;
            public Document LinkDocument;
            public Transform Transform;
            public string Title;
            public string Label;
            private List<LinkedTypeGroup> _groups;

            public static LinkInfo TryCreate(RevitLinkInstance instance)
            {
                try
                {
                    Document linkDocument = instance.GetLinkDocument();
                    if (linkDocument == null) return null;
                    return new LinkInfo
                    {
                        Instance = instance,
                        LinkDocument = linkDocument,
                        Transform = instance.GetTotalTransform(),
                        Title = linkDocument.Title,
                        Label = linkDocument.Title + "   (" + instance.Name + ")"
                    };
                }
                catch (Exception)
                {
                    return null;
                }
            }

            /// <summary>Linked lighting fixtures grouped by Family : Type (read once, cached).</summary>
            public List<LinkedTypeGroup> Groups
            {
                get
                {
                    if (_groups != null) return _groups;
                    List<FamilyInstance> fixtures = new FilteredElementCollector(LinkDocument)
                        .OfCategory(BuiltInCategory.OST_LightingFixtures)
                        .OfClass(typeof(FamilyInstance))
                        .Cast<FamilyInstance>()
                        .Where(fixture => fixture.Symbol != null && fixture.SuperComponent == null) // skip nested parts
                        .ToList();
                    _groups = fixtures
                        .GroupBy(fixture => fixture.Symbol.FamilyName + "\u0001" + TypeName(fixture.Symbol))
                        .Select(group => new LinkedTypeGroup
                        {
                            FamilyName = group.First().Symbol.FamilyName,
                            TypeName = TypeName(group.First().Symbol),
                            Instances = group.ToList()
                        })
                        .OrderBy(group => group.FamilyName, StringComparer.CurrentCultureIgnoreCase)
                        .ThenBy(group => group.TypeName, StringComparer.CurrentCultureIgnoreCase)
                        .ToList();
                    return _groups;
                }
            }

            private static string TypeName(FamilySymbol symbol)
            {
                Parameter parameter = symbol.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM);
                string name = parameter == null ? null : (parameter.AsString() ?? parameter.AsValueString());
                return string.IsNullOrWhiteSpace(name) ? symbol.Name : name;
            }
        }

        internal sealed class TargetFamily
        {
            public Family Family;
            public string CategoryName;
            public bool IsViewBased;
            public List<FamilySymbol> Symbols;
            public string Label { get { return CategoryName + ":  " + Family.Name; } }
            public override string ToString() { return Label; }

            public static List<TargetFamily> Collect(Document document)
            {
                var categories = new HashSet<long>(new[]
                {
                    BuiltInCategory.OST_LightingFixtures,
                    BuiltInCategory.OST_DetailComponents,
                    BuiltInCategory.OST_GenericAnnotation
                }.Select(category => (long)(int)category));

                var result = new List<TargetFamily>();
                foreach (Family family in new FilteredElementCollector(document).OfClass(typeof(Family)).Cast<Family>())
                {
                    Category category;
                    try { category = family.FamilyCategory; } catch (Exception) { continue; }
                    if (category == null || !categories.Contains(IdValue(category.Id))) continue;
                    List<FamilySymbol> symbols = family.GetFamilySymbolIds()
                        .Select(id => document.GetElement(id) as FamilySymbol)
                        .Where(symbol => symbol != null)
                        .OrderBy(symbol => symbol.Name, StringComparer.CurrentCultureIgnoreCase)
                        .ToList();
                    if (symbols.Count == 0) continue;
                    result.Add(new TargetFamily
                    {
                        Family = family,
                        CategoryName = category.Name,
                        IsViewBased = family.FamilyPlacementType == FamilyPlacementType.ViewBased,
                        Symbols = symbols
                    });
                }
                return result
                    .OrderBy(family => family.CategoryName, StringComparer.CurrentCultureIgnoreCase)
                    .ThenBy(family => family.Family.Name, StringComparer.CurrentCultureIgnoreCase)
                    .ToList();
            }
        }

        internal enum ChoiceKind { Existing, CreateNew, Skip }

        internal sealed class TargetChoice
        {
            public ChoiceKind Kind;
            public FamilySymbol Symbol;
            public string NewName;
            public override string ToString()
            {
                switch (Kind)
                {
                    case ChoiceKind.Existing: return Symbol.Name;
                    case ChoiceKind.CreateNew: return "+ New type: " + NewName;
                    default: return "(Skip - do not place)";
                }
            }
        }

        internal sealed class LinkedTypeGroup
        {
            public string FamilyName;
            public string TypeName;
            public List<FamilyInstance> Instances;
            public WpfComboBox Combo;
            public string Label { get { return FamilyName + " : " + TypeName; } }
            public TargetChoice SelectedChoice { get { return Combo == null ? null : Combo.SelectedItem as TargetChoice; } }
        }

        // ==================================================================
        // Window
        // ==================================================================

        internal sealed class SetupWindow : Window
        {
            private readonly List<LinkInfo> _links;
            private readonly List<TargetFamily> _families;
            private readonly View _activeView;
            private readonly WpfComboBox _linkCombo = new WpfComboBox { Padding = new Thickness(6), Margin = new Thickness(0, 4, 0, 4) };
            private readonly TextBlock _linkInfo = Hint();
            private readonly WpfTextBox _familySearch = new WpfTextBox { Padding = new Thickness(6), Margin = new Thickness(0, 4, 0, 6), ToolTip = "Type part of the family name" };
            private readonly WpfComboBox _familyCombo = new WpfComboBox { Padding = new Thickness(6), Margin = new Thickness(0, 0, 0, 4) };
            private readonly WpfComboBox _baseCombo = new WpfComboBox { Padding = new Thickness(6), Margin = new Thickness(0, 4, 0, 4) };
            private readonly TextBlock _familyInfo = Hint();
            private readonly WpfGrid _mapping = new WpfGrid();
            private readonly TextBlock _mappingInfo = Hint();
            private readonly WpfRadioButton _centre = new WpfRadioButton { Content = "Centre of the linked fixture (recommended)", IsChecked = true, GroupName = "pos", Margin = new Thickness(0, 3, 0, 3) };
            private readonly WpfRadioButton _insertion = new WpfRadioButton { Content = "Insertion point of the linked fixture", GroupName = "pos", Margin = new Thickness(0, 3, 0, 3) };
            private readonly WpfCheckBox _rotate = Check("Rotate symbols to match the linked fixtures", true);
            private readonly WpfCheckBox _skipDuplicates = Check("Skip positions that already have the same symbol (within 50 mm)", true);
            private readonly WpfCheckBox _comments = Check("Write link name, linked ID and linked type into Comments", true);
            private readonly WpfCheckBox _viewLevel = Check("View-specific families: only fixtures on the active view's level", true);

            public SetupWindow(List<LinkInfo> links, List<TargetFamily> families, View activeView, IntPtr owner)
            {
                _links = links;
                _families = families;
                _activeView = activeView;

                Title = ToolName;
                Width = 900;
                Height = 900;
                MinWidth = 760;
                MinHeight = 620;
                WindowStartupLocation = WindowStartupLocation.CenterOwner;
                Background = Brush("#F1F4F9");
                FontFamily = new FontFamily("Segoe UI");
                new WindowInteropHelper(this).Owner = owner;

                var root = new DockPanel();
                var header = new StackPanel { Background = Brush("#1E3A8A") };
                header.Children.Add(new TextBlock { Text = "LIGHT PLACEMENT 2D", Foreground = Brushes.White, FontSize = 21, FontWeight = FontWeights.SemiBold, Margin = new Thickness(20, 16, 20, 2) });
                header.Children.Add(new TextBlock { Text = "Place your 2D family at every lighting fixture of a linked model", Foreground = Brush("#DBEAFE"), Margin = new Thickness(20, 0, 20, 16) });
                DockPanel.SetDock(header, Dock.Top);
                root.Children.Add(header);

                var footer = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = WpfHorizontalAlignment.Right, Margin = new Thickness(16) };
                WpfButton cancel = MakeButton("Cancel", false);
                cancel.IsCancel = true;
                WpfButton run = MakeButton("Place symbols", true);
                run.IsDefault = true;
                run.Margin = new Thickness(8, 0, 0, 0);
                run.Click += RunClicked;
                footer.Children.Add(cancel);
                footer.Children.Add(run);
                DockPanel.SetDock(footer, Dock.Bottom);
                root.Children.Add(footer);

                var content = new StackPanel { Margin = new Thickness(20) };
                content.Children.Add(Card("1.  Linked model", BuildLinkCard()));
                content.Children.Add(Card("2.  2D family to place", BuildFamilyCard()));
                content.Children.Add(Card("3.  Review type matching", BuildMappingCard()));
                content.Children.Add(Card("4.  Options", BuildOptionsCard()));
                root.Children.Add(new ScrollViewer { Content = content, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
                Content = root;

                foreach (LinkInfo link in _links) _linkCombo.Items.Add(link.Label);
                _linkCombo.SelectionChanged += (sender, args) => RebuildMapping();
                _familySearch.TextChanged += (sender, args) => RefreshFamilyList();
                _familyCombo.SelectionChanged += (sender, args) => FamilyChanged();
                _baseCombo.DisplayMemberPath = "Name";

                RefreshFamilyList();
                _linkCombo.SelectedIndex = 0;
            }

            public LinkInfo SelectedLink { get { return _linkCombo.SelectedIndex < 0 ? null : _links[_linkCombo.SelectedIndex]; } }
            public TargetFamily SelectedFamily { get { return _familyCombo.SelectedItem as TargetFamily; } }
            public FamilySymbol BaseSymbol { get { return _baseCombo.SelectedItem as FamilySymbol; } }
            public List<LinkedTypeGroup> Groups { get { return SelectedLink == null ? new List<LinkedTypeGroup>() : SelectedLink.Groups; } }
            public bool UseCentre { get { return _centre.IsChecked == true; } }
            public bool Rotate { get { return _rotate.IsChecked == true; } }
            public bool SkipDuplicates { get { return _skipDuplicates.IsChecked == true; } }
            public bool WriteComments { get { return _comments.IsChecked == true; } }
            public bool OnlyActiveViewLevel { get { return _viewLevel.IsChecked == true; } }

            private UIElement BuildLinkCard()
            {
                var panel = new StackPanel();
                panel.Children.Add(Hint("Linked model that contains the lighting fixtures"));
                panel.Children.Add(_linkCombo);
                panel.Children.Add(_linkInfo);
                return panel;
            }

            private UIElement BuildFamilyCard()
            {
                var panel = new StackPanel();
                panel.Children.Add(Hint("Search your 2D families (Lighting Fixtures, Detail Items, Generic Annotations)"));
                panel.Children.Add(_familySearch);
                panel.Children.Add(_familyCombo);
                panel.Children.Add(Hint("New types are duplicated from this type of the chosen family:"));
                panel.Children.Add(_baseCombo);
                panel.Children.Add(_familyInfo);
                return panel;
            }

            private UIElement BuildMappingCard()
            {
                var panel = new StackPanel();
                panel.Children.Add(Hint("Each linked fixture type is matched to a type of your 2D family with the SAME name. "
                    + "Types you already created are used as they are; missing ones are created as new types. Change any row before placing."));
                var buttons = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 8, 0, 8) };
                WpfButton match = MakeButton("Match by name again", false);
                match.Click += (sender, args) => RebuildMapping();
                WpfButton allNew = MakeButton("Unmatched = new type", false);
                allNew.Margin = new Thickness(6, 0, 0, 0);
                allNew.Click += (sender, args) => SetUnmatched(ChoiceKind.CreateNew);
                WpfButton allSkip = MakeButton("Unmatched = skip", false);
                allSkip.Margin = new Thickness(6, 0, 0, 0);
                allSkip.Click += (sender, args) => SetUnmatched(ChoiceKind.Skip);
                buttons.Children.Add(match);
                buttons.Children.Add(allNew);
                buttons.Children.Add(allSkip);
                panel.Children.Add(buttons);
                panel.Children.Add(new Border
                {
                    BorderBrush = Brush("#CBD5E1"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(4),
                    Child = new ScrollViewer { Content = _mapping, MaxHeight = 320, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(8) }
                });
                panel.Children.Add(_mappingInfo);
                return panel;
            }

            private UIElement BuildOptionsCard()
            {
                var panel = new StackPanel();
                panel.Children.Add(Hint("Place each symbol at:"));
                panel.Children.Add(_centre);
                panel.Children.Add(_insertion);
                panel.Children.Add(_rotate);
                panel.Children.Add(_skipDuplicates);
                panel.Children.Add(_comments);
                panel.Children.Add(_viewLevel);
                return panel;
            }

            private void RefreshFamilyList()
            {
                TargetFamily previous = SelectedFamily;
                string query = (_familySearch.Text ?? string.Empty).Trim();
                _familyCombo.Items.Clear();
                foreach (TargetFamily family in _families)
                {
                    if (query.Length > 0 && family.Label.IndexOf(query, StringComparison.CurrentCultureIgnoreCase) < 0) continue;
                    _familyCombo.Items.Add(family);
                }
                if (previous != null && _familyCombo.Items.Contains(previous)) _familyCombo.SelectedItem = previous;
                else if (_familyCombo.Items.Count > 0) _familyCombo.SelectedIndex = 0;
                else FamilyChanged();
            }

            private void FamilyChanged()
            {
                TargetFamily family = SelectedFamily;
                _baseCombo.Items.Clear();
                if (family == null)
                {
                    _familyInfo.Text = "No family matches the search.";
                    RebuildMapping();
                    return;
                }
                foreach (FamilySymbol symbol in family.Symbols) _baseCombo.Items.Add(symbol);
                _baseCombo.SelectedIndex = 0;
                _familyInfo.Text = family.Symbols.Count + " existing type(s). " + (family.IsViewBased
                    ? "View-specific family: symbols are placed in the ACTIVE PLAN VIEW (" + (_activeView == null ? "none" : _activeView.Name) + ")."
                    : "Model family: symbols are placed at each fixture's height on the nearest level below.");
                RebuildMapping();
            }

            private void RebuildMapping()
            {
                _mapping.Children.Clear();
                _mapping.RowDefinitions.Clear();
                _mapping.ColumnDefinitions.Clear();
                foreach (double width in new[] { 2.2, 0.6, 2.4 })
                    _mapping.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(width, GridUnitType.Star) });

                LinkInfo link = SelectedLink;
                TargetFamily family = SelectedFamily;
                if (link == null) return;
                List<LinkedTypeGroup> groups = link.Groups;
                _linkInfo.Text = groups.Sum(group => group.Instances.Count) + " lighting fixture(s) in " + groups.Count + " type(s).";
                if (family == null || groups.Count == 0)
                {
                    _mappingInfo.Text = groups.Count == 0 ? "The selected link has no lighting fixtures." : "Choose a 2D family.";
                    return;
                }

                AddRow(0, Bold("Linked fixture (Family : Type)"), Bold("Qty"), Bold("Place as type of " + family.Family.Name));

                // Type names used by more than one linked family get "Family - Type" to stay unique.
                var shared = new HashSet<string>(groups.GroupBy(group => group.TypeName, StringComparer.CurrentCultureIgnoreCase)
                    .Where(group => group.Select(item => item.FamilyName).Distinct().Count() > 1)
                    .Select(group => group.Key), StringComparer.CurrentCultureIgnoreCase);

                int row = 1;
                foreach (LinkedTypeGroup group in groups)
                {
                    string newName = CleanName(shared.Contains(group.TypeName) ? group.FamilyName + " - " + group.TypeName : group.TypeName);
                    var combo = new WpfComboBox { Padding = new Thickness(4), Margin = new Thickness(0, 2, 0, 2) };
                    var create = new TargetChoice { Kind = ChoiceKind.CreateNew, NewName = newName };
                    combo.Items.Add(create);
                    TargetChoice match = null;
                    foreach (FamilySymbol symbol in family.Symbols)
                    {
                        var existing = new TargetChoice { Kind = ChoiceKind.Existing, Symbol = symbol };
                        combo.Items.Add(existing);
                        if (match == null && (Same(symbol.Name, group.TypeName) || Same(symbol.Name, newName) || Same(symbol.Name, group.FamilyName + " - " + group.TypeName)))
                            match = existing;
                    }
                    combo.Items.Add(new TargetChoice { Kind = ChoiceKind.Skip });
                    combo.SelectedItem = match ?? create;
                    combo.SelectionChanged += (sender, args) => UpdateMappingInfo();
                    group.Combo = combo;

                    AddRow(row++, new TextBlock { Text = group.Label, TextWrapping = TextWrapping.Wrap, VerticalAlignment = WpfVerticalAlignment.Center },
                        new TextBlock { Text = group.Instances.Count.ToString(CultureInfo.CurrentCulture), VerticalAlignment = WpfVerticalAlignment.Center },
                        combo);
                }
                UpdateMappingInfo();
            }

            private void AddRow(int row, UIElement first, UIElement second, UIElement third)
            {
                _mapping.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                int column = 0;
                foreach (UIElement element in new[] { first, second, third })
                {
                    var frameworkElement = element as FrameworkElement;
                    if (frameworkElement != null && column < 2) frameworkElement.Margin = new Thickness(0, 2, 8, 2);
                    WpfGrid.SetRow(element, row);
                    WpfGrid.SetColumn(element, column++);
                    _mapping.Children.Add(element);
                }
            }

            private void SetUnmatched(ChoiceKind kind)
            {
                foreach (LinkedTypeGroup group in Groups)
                {
                    if (group.Combo == null) continue;
                    TargetChoice current = group.SelectedChoice;
                    if (current != null && current.Kind == ChoiceKind.Existing) continue;
                    group.Combo.SelectedItem = group.Combo.Items.OfType<TargetChoice>().First(choice => choice.Kind == kind);
                }
                UpdateMappingInfo();
            }

            private void UpdateMappingInfo()
            {
                List<LinkedTypeGroup> groups = Groups.Where(group => group.Combo != null).ToList();
                int existing = groups.Count(group => group.SelectedChoice != null && group.SelectedChoice.Kind == ChoiceKind.Existing);
                int created = groups.Count(group => group.SelectedChoice != null && group.SelectedChoice.Kind == ChoiceKind.CreateNew);
                int skipped = groups.Count - existing - created;
                int fixtures = groups.Where(group => group.SelectedChoice != null && group.SelectedChoice.Kind != ChoiceKind.Skip).Sum(group => group.Instances.Count);
                _mappingInfo.Text = existing + " type(s) use an existing type, " + created + " will be created, " + skipped + " skipped. "
                    + fixtures + " symbol(s) will be placed.";
            }

            private void RunClicked(object sender, RoutedEventArgs args)
            {
                string problem = null;
                if (SelectedLink == null) problem = "Choose a linked model.";
                else if (SelectedFamily == null) problem = "Choose a 2D family.";
                else if (BaseSymbol == null) problem = "Choose the type that new types are duplicated from.";
                else if (Groups.Count == 0) problem = "The selected link has no lighting fixtures.";
                else if (Groups.All(group => group.SelectedChoice == null || group.SelectedChoice.Kind == ChoiceKind.Skip)) problem = "Every type is set to Skip - nothing to place.";
                else if (SelectedFamily.IsViewBased && !(_activeView is ViewPlan))
                    problem = "This is a view-specific family (detail item / annotation). Open a floor or ceiling plan view first, then run the tool again.";
                if (problem != null)
                {
                    WpfMessageBox.Show(this, problem, ToolName, MessageBoxButton.OK, MessageBoxImage.Warning);
                    return;
                }
                DialogResult = true;
            }

            private static bool Same(string a, string b)
            {
                return string.Equals((a ?? string.Empty).Trim(), (b ?? string.Empty).Trim(), StringComparison.CurrentCultureIgnoreCase);
            }

            private static TextBlock Hint(string text = null)
            {
                return new TextBlock { Text = text ?? string.Empty, Foreground = Brush("#475569"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 2, 0, 2) };
            }

            private static TextBlock Bold(string text)
            {
                return new TextBlock { Text = text, FontWeight = FontWeights.SemiBold, Foreground = Brush("#0F172A"), Margin = new Thickness(0, 0, 8, 6), TextWrapping = TextWrapping.Wrap };
            }

            private static WpfCheckBox Check(string text, bool isChecked)
            {
                return new WpfCheckBox { Content = text, IsChecked = isChecked, Margin = new Thickness(0, 4, 0, 4) };
            }

            private static Border Card(string title, UIElement content)
            {
                var stack = new StackPanel();
                stack.Children.Add(new TextBlock { Text = title, FontSize = 14, FontWeight = FontWeights.SemiBold, Foreground = Brush("#0F172A"), Margin = new Thickness(0, 0, 0, 8) });
                stack.Children.Add(content);
                return new Border { Background = Brushes.White, BorderBrush = Brush("#E2E8F0"), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8), Padding = new Thickness(14), Margin = new Thickness(0, 0, 0, 12), Child = stack };
            }

            private static WpfButton MakeButton(string text, bool primary)
            {
                return new WpfButton
                {
                    Content = text,
                    MinWidth = primary ? 130 : 90,
                    Height = 32,
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
}
