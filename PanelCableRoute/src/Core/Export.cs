// -----------------------------------------------------------------------------
// The exported table and the text report. Both are built here as plain
// strings, so they can be tested without Revit, Excel or a file system.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace PanelCableRoute.Core
{
    public static class Export
    {
        public static readonly string[] Header = new string[]
        {
            "Connection", "Source panel", "Source level", "Destination",
            "Destination level", "Circuit", "Load name", "Tray route (m)",
            "Source connection (m)", "Destination connection (m)",
            "Bridged tray gap (m)", "Route length (m)", "With allowance (m)",
            "Vertical difference (m)", "Current (A)", "Current from",
            "Voltage (V)", "mV/A/m", "Voltage drop (V)", "Voltage drop (%)",
            "Limit (%)", "Status", "Source ID", "Destination ID"
        };

        private static string Number(double value, int decimals)
        {
            return value.ToString(
                "0." + new string('#', decimals), CultureInfo.InvariantCulture);
        }

        /// <summary>One row of values per route, in the Header's order.</summary>
        public static List<string[]> Rows(
            IEnumerable<RouteRecord> routes, RouteOptions options)
        {
            List<string[]> rows = new List<string[]>();
            foreach (RouteRecord route in routes)
            {
                rows.Add(new string[]
                {
                    RouteKindText.Of(route.Kind),
                    route.Source.Name,
                    route.Source.LevelName,
                    route.Destination.Name,
                    route.Destination.LevelName,
                    route.CircuitNumber,
                    route.LoadName,
                    Number(Units.FeetToMetres(route.TrayLength), 3),
                    Number(Units.FeetToMetres(route.SourceGap), 3),
                    Number(Units.FeetToMetres(route.DestinationGap), 3),
                    Number(Units.FeetToMetres(route.Bridge), 3),
                    Number(Units.FeetToMetres(route.Total), 3),
                    Number(Units.FeetToMetres(route.WithAllowance), 3),
                    Number(Units.FeetToMetres(route.VerticalDifference), 3),
                    Number(route.Current, 2),
                    route.CurrentFromCircuit ? "Revit circuit" : "dialog",
                    Number(route.Voltage, 1),
                    Number(options.MilliVoltsPerAmpMetre, 3),
                    Number(route.DropVolts, 2),
                    Number(route.DropPercent, 3),
                    Number(options.LimitPercent, 2),
                    route.WithinLimit ? "OK" : "OVER LIMIT",
                    route.Source.Id.ToString(CultureInfo.InvariantCulture),
                    route.Destination.Id.ToString(CultureInfo.InvariantCulture)
                });
            }
            return rows;
        }

        /// <summary>One CSV field, quoted when it has to be.</summary>
        public static string CsvField(string value)
        {
            string text = value == null ? "" : value;
            if (text.IndexOf(',') >= 0 || text.IndexOf('"') >= 0
                || text.IndexOf('\n') >= 0 || text.IndexOf('\r') >= 0)
            {
                return "\"" + text.Replace("\"", "\"\"") + "\"";
            }
            return text;
        }

        public static string Csv(IList<string[]> rows)
        {
            StringBuilder builder = new StringBuilder();
            AppendDelimited(builder, Header, ",", true);
            for (int index = 0; index < rows.Count; index++)
            {
                AppendDelimited(builder, rows[index], ",", true);
            }
            return builder.ToString();
        }

        /// <summary>Tab separated, for pasting straight into a spreadsheet.</summary>
        public static string Tabbed(IList<string[]> rows)
        {
            StringBuilder builder = new StringBuilder();
            AppendDelimited(builder, Header, "\t", false);
            for (int index = 0; index < rows.Count; index++)
            {
                AppendDelimited(builder, rows[index], "\t", false);
            }
            return builder.ToString();
        }

        private static void AppendDelimited(
            StringBuilder builder, string[] values, string separator,
            bool quote)
        {
            for (int index = 0; index < values.Length; index++)
            {
                if (index > 0)
                {
                    builder.Append(separator);
                }
                builder.Append(quote ? CsvField(values[index]) : values[index]);
            }
            builder.Append("\r\n");
        }

        /// <summary>The overview text, also used for the clipboard.</summary>
        public static List<string> Report(
            RouteResult result, RouteOptions options, TrayNetwork network)
        {
            List<string> lines = new List<string>();
            int circuits = 0;
            int isolators = 0;
            int pairs = 0;
            int over = 0;
            int bridged = 0;
            RouteRecord longest = null;
            RouteRecord worst = null;
            for (int index = 0; index < result.Routes.Count; index++)
            {
                RouteRecord route = result.Routes[index];
                if (route.Kind == RouteKind.Circuit)
                {
                    circuits++;
                }
                else if (route.Kind == RouteKind.Isolator)
                {
                    isolators++;
                }
                else
                {
                    pairs++;
                }
                if (!route.WithinLimit)
                {
                    over++;
                }
                if (route.Bridge > Geometry.Epsilon)
                {
                    bridged++;
                }
                if (longest == null || route.WithAllowance > longest.WithAllowance)
                {
                    longest = route;
                }
                if (worst == null || route.DropPercent > worst.DropPercent)
                {
                    worst = route;
                }
            }

            lines.Add("Panels measured: " + result.Panels.Count.ToString());
            lines.Add(string.Format(CultureInfo.InvariantCulture,
                "Routes: {0}  ({1} circuit, {2} isolator, {3} panel pair)",
                result.Routes.Count, circuits, isolators, pairs));
            lines.Add("");
            lines.Add(string.Format(CultureInfo.InvariantCulture,
                "Tray network: {0} tray(s), {1} node(s), {2} fitting(s) "
                + "bridging tray ends, {3} mm connection tolerance.",
                network.TrayCount, network.NodeCount,
                network.FittingsBridged, Number(options.ToleranceMm, 2)));
            if (network.BridgeEdgeCount > 0)
            {
                lines.Add(string.Format(CultureInfo.InvariantCulture,
                    "  The trays were modelled as separate pieces: {0} hop(s) "
                    + "of up to {1} mm were added to join them up, {2:0.00} m "
                    + "in total. Routes follow the trays and cross these gaps "
                    + "only where they must.",
                    network.BridgeEdgeCount, Number(options.MaxBridgeMm, 0),
                    Units.FeetToMetres(network.BridgeLengthAdded)));
            }
            if (network.ComponentCount > 1)
            {
                lines.Add(string.Format(CultureInfo.InvariantCulture,
                    "  WARNING: the trays still fall into {0} separate "
                    + "group(s) that do not join up. Routes between groups "
                    + "have to cross open space. Raise the connection "
                    + "tolerance or the gap-joining distance, or join the "
                    + "trays in the model.", network.ComponentCount));
            }
            lines.Add(string.Format(CultureInfo.InvariantCulture,
                "Off-tray travel costs {0} times its length, so a route "
                + "follows the trays unless there is no tray path at all.",
                Number(options.TrayPreference, 2)));
            lines.Add("");
            lines.Add(string.Format(CultureInfo.InvariantCulture,
                "Cable allowance: {0} %  (the drop is worked out on the "
                + "length with allowance)", Number(options.AllowancePercent, 2)));
            lines.Add(string.Format(CultureInfo.InvariantCulture,
                "Cable: {0} mV/A/m, limit {1} % of the nominal voltage",
                Number(options.MilliVoltsPerAmpMetre, 3),
                Number(options.LimitPercent, 2)));
            lines.Add(string.Format(CultureInfo.InvariantCulture,
                "Fallbacks when a circuit has no figures: {0} A, {1} V",
                Number(options.Current, 2), Number(options.Voltage, 1)));
            lines.Add("");
            if (longest != null)
            {
                lines.Add(string.Format(CultureInfo.InvariantCulture,
                    "Longest route: {0:0.00} m  {1}",
                    Units.FeetToMetres(longest.WithAllowance), longest.Title));
                lines.Add(string.Format(CultureInfo.InvariantCulture,
                    "Worst voltage drop: {0:0.00} % ({1:0.0} V on {2:0} V)  {3}",
                    worst.DropPercent, worst.DropVolts, worst.Voltage,
                    worst.Title));
                lines.Add(string.Format(CultureInfo.InvariantCulture,
                    "Over the {0} % limit: {1} route(s)",
                    Number(options.LimitPercent, 2), over));
            }
            lines.Add("");
            lines.Add("Checks");
            lines.Add("  Equipment not reached : "
                + (result.Unreachable.Count == 0
                    ? "none" : result.Unreachable.Count.ToString()));
            lines.Add("  Routes bridging a gap in the modelled trays: "
                + (bridged == 0 ? "none" : bridged.ToString()));
            if (result.Cancelled)
            {
                lines.Add("");
                lines.Add("The measuring run was cancelled, so this is a "
                    + "partial set of routes.");
            }
            return lines;
        }
    }
}
