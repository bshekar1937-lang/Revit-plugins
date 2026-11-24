// -----------------------------------------------------------------------------
// The command behind the ribbon button: read the model, measure, preview, then
// draw or export if asked.
//
// TransactionMode.Manual, because nothing should be in a transaction while the
// dialogs are open; the drawing step opens its own.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Windows.Interop;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using PanelCableRoute.Core;
using PanelCableRoute.Revit;
using PanelCableRoute.UI;

namespace PanelCableRoute.Commands
{
    [Transaction(TransactionMode.Manual)]
    public class PanelRouteCommand : IExternalCommand
    {
        private const string Title = "Panel Cable Route Pro";

        /// <summary>
        /// Remembered between runs in the same Revit session. Settings that
        /// should outlive Revit belong in a file, which this does not do yet.
        /// </summary>
        private static RouteOptions _remembered = new RouteOptions();

        public Result Execute(
            ExternalCommandData commandData, ref string message,
            ElementSet elements)
        {
            UIApplication uiApplication = commandData.Application;
            UIDocument uiDocument = uiApplication.ActiveUIDocument;
            if (uiDocument == null)
            {
                message = "Open a project first.";
                return Result.Cancelled;
            }
            Document doc = uiDocument.Document;
            View activeView = doc.ActiveView;

            if (!RouteDrawing.IsPlanView(activeView))
            {
                TaskDialog.Show(Title,
                    "Open the plan view you want the routes drawn in before "
                    + "running this tool (floor plan, ceiling plan or "
                    + "engineering plan).");
                return Result.Cancelled;
            }

            try
            {
                // ---- 1. what is in the model ----------------------------
                List<EquipmentItem> panels = ModelReader.ReadPanels(doc);
                if (panels.Count == 0)
                {
                    TaskDialog.Show(Title,
                        "No electrical equipment was found, so there are no "
                        + "panels to measure between.");
                    return Result.Cancelled;
                }
                Dictionary<long, EquipmentItem> isolators =
                    ModelReader.ReadIsolators(doc);
                List<TraySegment> trays;
                List<FittingLink> fittings;
                ModelReader.ReadTrays(doc, out trays, out fittings);
                if (trays.Count == 0)
                {
                    TaskDialog.Show(Title,
                        "No cable trays were found in this model. The routes "
                        + "follow the modelled tray centre lines, so there is "
                        + "nothing to measure along.");
                    return Result.Cancelled;
                }

                // ---- 2. the dialog ---------------------------------------
                SetupWindow setup = new SetupWindow(
                    _remembered, panels, isolators.Count, trays.Count);
                Owned(setup, uiApplication);
                setup.ShowDialog();
                if (setup.Options == null)
                {
                    return Result.Cancelled;
                }
                RouteOptions options = setup.Options;
                _remembered = options.Copy();

                // ---- 3. the network and the electrical topology ----------
                TrayNetwork network = new TrayNetwork(
                    trays, fittings, options.ToleranceMm);
                // Real models often have the trays in separate pieces. Join
                // the pieces that nearly touch, and make off-tray travel
                // expensive, so routes follow the trays instead of striking
                // out across the building.
                network.BridgeGaps(
                    options.MaxBridgeMm, options.TrayPreference);
                if (network.NodeCount == 0)
                {
                    TaskDialog.Show(Title,
                        "The cable trays in this model gave no usable centre "
                        + "lines.");
                    return Result.Cancelled;
                }

                RouteInput input = new RouteInput();
                input.Network = network;
                input.Options = options;
                input.Panels = panels;
                input.IsolatorsById = isolators;
                input.Circuits = ModelReader.ReadCircuits(doc);

                Dictionary<long, EquipmentItem> known =
                    new Dictionary<long, EquipmentItem>();
                for (int index = 0; index < panels.Count; index++)
                {
                    known[panels[index].Id] = panels[index];
                }
                foreach (KeyValuePair<long, EquipmentItem> entry in isolators)
                {
                    known[entry.Key] = entry.Value;
                }
                List<long> memberIds = new List<long>();
                for (int index = 0; index < input.Circuits.Count; index++)
                {
                    memberIds.Add(input.Circuits[index].MemberId);
                }
                input.ItemsById = ModelReader.ReadCircuitMembers(
                    doc, memberIds, known);

                List<long> wiringIds = new List<long>(memberIds);
                foreach (KeyValuePair<long, EquipmentItem> entry in isolators)
                {
                    wiringIds.Add(entry.Key);
                }
                input.Wiring = options.IncludeIsolators
                    ? ModelReader.ReadWiring(doc, wiringIds)
                    : new Dictionary<long, List<long>>();

                // ---- 4. measure ------------------------------------------
                RouteResult result;
                ProgressWindow progress = new ProgressWindow();
                Owned(progress, uiApplication);
                progress.Show();
                try
                {
                    result = RouteBuilder.Build(input, progress.Report);
                }
                finally
                {
                    progress.Close();
                }

                if (result.Routes.Count == 0)
                {
                    TaskDialog.Show(Title, string.Format(
                        CultureInfo.CurrentCulture,
                        "No routes could be measured. Check that the panels "
                        + "sit near the modelled cable trays, and that the "
                        + "trays are connected (the connection tolerance is "
                        + "{0:0.##} mm).", options.ToleranceMm));
                    return Result.Cancelled;
                }

                // ---- 5. preview, then draw or export ---------------------
                ResultWindow window = new ResultWindow(
                    result, options, trays, network);
                window.SelectInRevit = delegate (List<long> ids, bool zoom)
                {
                    return Focus(uiDocument, ids, zoom);
                };
                Owned(window, uiApplication);
                window.ShowDialog();

                DrawingSummary drawing = null;
                if (window.Choice == ResultChoice.Draw)
                {
                    List<RouteRecord> toDraw = new List<RouteRecord>();
                    for (int index = 0; index < result.Routes.Count; index++)
                    {
                        if (result.Routes[index].Include)
                        {
                            toDraw.Add(result.Routes[index]);
                        }
                    }
                    drawing = RouteDrawing.Draw(doc, activeView, toDraw);
                }

                Report(result, options, drawing, window.Exported);
                return Result.Succeeded;
            }
            catch (Exception error)
            {
                message = error.Message;
                TaskDialog.Show(Title,
                    "The tool stopped with an error and nothing was changed:"
                    + Environment.NewLine + Environment.NewLine
                    + error.ToString());
                return Result.Failed;
            }
        }

        /// <summary>
        /// Make a dialog behave as a child of the Revit window, so it stays in
        /// front and Revit does not accept input behind it.
        /// </summary>
        private static void Owned(
            System.Windows.Window window, UIApplication application)
        {
            try
            {
                WindowInteropHelper helper = new WindowInteropHelper(window);
                helper.Owner = application.MainWindowHandle;
            }
            catch (Exception)
            {
                // Worst case the dialog is simply not owned.
            }
        }

        /// <summary>Select, and optionally zoom to, elements in Revit.</summary>
        private static string Focus(
            UIDocument uiDocument, List<long> ids, bool zoom)
        {
            try
            {
                List<ElementId> elementIds = new List<ElementId>();
                for (int index = 0; index < ids.Count; index++)
                {
                    ElementId id = ModelReader.ToElementId(ids[index]);
                    if (uiDocument.Document.GetElement(id) != null)
                    {
                        elementIds.Add(id);
                    }
                }
                if (elementIds.Count == 0)
                {
                    return "Those elements are no longer in the model.";
                }
                uiDocument.Selection.SetElementIds(elementIds);
                if (zoom)
                {
                    uiDocument.ShowElements(elementIds);
                }
                return elementIds.Count.ToString(CultureInfo.CurrentCulture)
                    + " element(s) "
                    + (zoom ? "zoomed to and selected" : "selected")
                    + " in Revit - move or close this window to look at them.";
            }
            catch (Exception error)
            {
                return "Revit could not show them: " + error.Message;
            }
        }

        private static void Report(
            RouteResult result, RouteOptions options,
            DrawingSummary drawing, List<string> exported)
        {
            double worst = 0.0;
            int over = 0;
            for (int index = 0; index < result.Routes.Count; index++)
            {
                RouteRecord route = result.Routes[index];
                if (route.DropPercent > worst)
                {
                    worst = route.DropPercent;
                }
                if (!route.WithinLimit)
                {
                    over++;
                }
            }

            StringBuilder text = new StringBuilder();
            text.AppendLine(string.Format(CultureInfo.CurrentCulture,
                "{0} route(s) measured from {1} panel(s).",
                result.Routes.Count, result.Panels.Count));
            text.AppendLine(string.Format(CultureInfo.CurrentCulture,
                "Worst voltage drop: {0:0.00} % ({1} over the {2:0.##} % "
                + "limit).", worst, over, options.LimitPercent));
            if (result.Unreachable.Count > 0)
            {
                text.AppendLine(string.Format(CultureInfo.CurrentCulture,
                    "{0} item(s) could not be reached through the trays.",
                    result.Unreachable.Count));
            }
            if (result.Cancelled)
            {
                text.AppendLine(
                    "The run was cancelled part way, so this is a partial set.");
            }
            if (drawing != null)
            {
                text.AppendLine();
                if (drawing.Committed)
                {
                    text.AppendLine(string.Format(CultureInfo.CurrentCulture,
                        "{0} detail line segment(s) drawn across {1} view(s); "
                        + "{2} line(s) from a previous run were replaced.",
                        drawing.Created, drawing.Views, drawing.Deleted));
                    if (drawing.Errors.Count > 0)
                    {
                        text.AppendLine(string.Format(
                            CultureInfo.CurrentCulture,
                            "{0} segment(s) could not be drawn. First: {1}",
                            drawing.Errors.Count, drawing.Errors[0]));
                    }
                    if (drawing.MissingLevels.Count > 0)
                    {
                        text.AppendLine(
                            "No plan view was found for some end levels, so "
                            + "those routes were drawn only in the views that "
                            + "do exist.");
                    }
                }
                else
                {
                    text.AppendLine("The detail lines could not be drawn and "
                        + "nothing was changed: " + drawing.Failure);
                }
            }
            if (exported != null && exported.Count > 0)
            {
                text.AppendLine();
                text.AppendLine("Exported to:");
                for (int index = 0; index < exported.Count; index++)
                {
                    text.AppendLine("  " + exported[index]);
                }
            }
            TaskDialog.Show(Title, text.ToString());
        }
    }
}
