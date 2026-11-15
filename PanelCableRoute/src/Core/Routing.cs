// -----------------------------------------------------------------------------
// Measuring one route, and the record that comes out of it.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;

namespace PanelCableRoute.Core
{
    /// <summary>What the router found between two attached items.</summary>
    public class RouteMeasurement
    {
        public double Total;
        public double NetworkLength;
        public Attachment Source;
        public Attachment Destination;
        public List<NetworkEdge> PathEdges = new List<NetworkEdge>();
        /// <summary>Straight bridge used where the modelled trays do not join.</summary>
        public double Bridge;
        public List<Pt> Points = new List<Pt>();
    }

    public static class Router
    {
        /// <summary>
        /// The cheapest route from an item whose reach has been worked out to
        /// an item's attachments. Returns null when neither a path nor a
        /// bridge can be formed.
        /// </summary>
        public static RouteMeasurement Measure(
            TrayNetwork network, Reach reach,
            IList<Attachment> sourceAttachments,
            IList<Attachment> destinationAttachments)
        {
            if (sourceAttachments == null || sourceAttachments.Count == 0
                || destinationAttachments == null
                || destinationAttachments.Count == 0)
            {
                return null;
            }
            Dictionary<int, Attachment> byNode = new Dictionary<int, Attachment>();
            for (int index = 0; index < sourceAttachments.Count; index++)
            {
                byNode[sourceAttachments[index].Node] = sourceAttachments[index];
            }

            Attachment bestDestination = null;
            double bestTotal = 0.0;
            for (int index = 0; index < destinationAttachments.Count; index++)
            {
                Attachment destination = destinationAttachments[index];
                double distance;
                if (!reach.Distances.TryGetValue(destination.Node, out distance))
                {
                    continue;
                }
                double total = distance + destination.WeightedCost;
                if (bestDestination == null || total < bestTotal)
                {
                    bestDestination = destination;
                    bestTotal = total;
                }
            }

            if (bestDestination != null)
            {
                int seed;
                Attachment source = null;
                if (reach.Seeds.TryGetValue(bestDestination.Node, out seed))
                {
                    byNode.TryGetValue(seed, out source);
                }
                if (source != null)
                {
                    RouteMeasurement measurement = new RouteMeasurement();
                    measurement.Source = source;
                    measurement.Destination = bestDestination;
                    measurement.PathEdges =
                        network.PathTo(reach, bestDestination.Node);
                    // The distance Dijkstra worked with is weighted, because
                    // a hop through the air costs more than its length. The
                    // reported lengths are the real ones, added up along the
                    // path that preference chose.
                    double trayLength = 0.0;
                    double bridgeLength = 0.0;
                    for (int index = 0;
                         index < measurement.PathEdges.Count;
                         index++)
                    {
                        NetworkEdge edge = measurement.PathEdges[index];
                        if (edge.Kind == EdgeKind.Bridge)
                        {
                            bridgeLength += edge.RealLength;
                        }
                        else
                        {
                            trayLength += edge.RealLength;
                        }
                    }
                    measurement.NetworkLength = trayLength;
                    measurement.Bridge = bridgeLength;
                    measurement.Total = source.Cost + trayLength
                        + bridgeLength + bestDestination.Cost;
                    measurement.Points = Polyline(
                        network, source, bestDestination,
                        measurement.PathEdges, false);
                    return measurement;
                }
            }

            // No path at all, even across the bridged gaps: keep both tray
            // legs and cross the remaining distance in one hop. This is the
            // last resort and shows up in the preview as a bridged route.
            Attachment fallbackSource = null;
            Attachment fallbackDestination = null;
            double fallbackTotal = 0.0;
            double fallbackGap = 0.0;
            double tie = Units.MmToFeet(TrayNetwork.TieToleranceMm);
            for (int first = 0; first < sourceAttachments.Count; first++)
            {
                Attachment source = sourceAttachments[first];
                for (int second = 0;
                     second < destinationAttachments.Count;
                     second++)
                {
                    Attachment destination = destinationAttachments[second];
                    double gap = Geometry.OrthogonalDistance(
                        network.NodePoint(source.Node),
                        network.NodePoint(destination.Node));
                    double total = source.Cost + gap + destination.Cost;
                    // Several combinations usually come to the same total.
                    // Prefer the shortest bridge, so the gap reported is the
                    // real break in the trays rather than a diagonal across
                    // the building. The totals are sums of floating point
                    // values and never match to the last bit, hence the
                    // tolerance.
                    bool better = fallbackSource == null
                        || total < fallbackTotal - tie
                        || (total < fallbackTotal + tie && gap < fallbackGap);
                    if (better)
                    {
                        fallbackSource = source;
                        fallbackDestination = destination;
                        fallbackTotal = total;
                        fallbackGap = gap;
                    }
                }
            }
            if (fallbackSource == null)
            {
                return null;
            }
            RouteMeasurement bridged = new RouteMeasurement();
            bridged.Total = fallbackTotal;
            bridged.NetworkLength = 0.0;
            bridged.Source = fallbackSource;
            bridged.Destination = fallbackDestination;
            bridged.PathEdges = new List<NetworkEdge>();
            bridged.Bridge = fallbackGap;
            bridged.Points = Polyline(
                network, fallbackSource, fallbackDestination,
                bridged.PathEdges, true);
            return bridged;
        }

        /// <summary>Every point of the drawn route, source to destination.</summary>
        private static List<Pt> Polyline(
            TrayNetwork network, Attachment source, Attachment destination,
            IList<NetworkEdge> edges, bool bridge)
        {
            List<Pt> points = Geometry.OrthogonalPoints(
                source.EquipmentPoint, source.TrayPoint);
            points.AddRange(source.PartialPoints);
            points.AddRange(network.EdgePoints(edges));
            if (bridge)
            {
                List<Pt> across = Geometry.OrthogonalPoints(
                    network.NodePoint(source.Node),
                    network.NodePoint(destination.Node));
                for (int index = 1; index < across.Count; index++)
                {
                    points.Add(across[index]);
                }
            }
            for (int index = destination.PartialPoints.Length - 1;
                 index >= 0;
                 index--)
            {
                points.Add(destination.PartialPoints[index]);
            }
            points.AddRange(Geometry.OrthogonalPoints(
                destination.TrayPoint, destination.EquipmentPoint));
            return points;
        }
    }

    public static class VoltageDrop
    {
        /// <summary>
        /// The usual cable-table sum: mV/A/m x current x length / 1000.
        /// Three-phase tables already allow for the phase configuration.
        /// </summary>
        public static double Volts(
            double lengthMetres, double current, double milliVoltsPerAmpMetre)
        {
            return milliVoltsPerAmpMetre * current * lengthMetres / 1000.0;
        }

        /// <summary>The cable length at which the limit is reached, in metres.</summary>
        public static double ReachMetres(
            double voltage, double limitPercent, double current,
            double milliVoltsPerAmpMetre)
        {
            double perMetre = milliVoltsPerAmpMetre * current / 1000.0;
            if (perMetre <= Geometry.Epsilon)
            {
                return 0.0;
            }
            return voltage * limitPercent / 100.0 / perMetre;
        }
    }

    /// <summary>One measured connection, ready for the table and the export.</summary>
    public class RouteRecord
    {
        public RouteKind Kind;
        public EquipmentItem Source;
        public EquipmentItem Destination;
        public string CircuitNumber = "";
        public string LoadName = "";
        public double Current;
        public bool CurrentFromCircuit;
        public double Voltage;

        /// <summary>Along the trays, in feet.</summary>
        public double TrayLength;
        public double SourceGap;
        public double DestinationGap;
        public double Bridge;
        public double Total;
        public double WithAllowance;
        public double VerticalDifference;

        public double DropVolts;
        public double DropPercent;
        public bool WithinLimit;

        public List<Pt> Points = new List<Pt>();
        /// <summary>Ticked in the table: drawn and exported.</summary>
        public bool Include = true;

        public string Title
        {
            get { return Source.Name + "  ->  " + Destination.Name; }
        }

        public static RouteRecord Create(
            RouteKind kind, EquipmentItem source, EquipmentItem destination,
            RouteMeasurement measurement, RouteOptions options,
            CircuitLink circuit)
        {
            RouteRecord record = new RouteRecord();
            record.Kind = kind;
            record.Source = source;
            record.Destination = destination;
            record.TrayLength = measurement.Source.PartialLength
                + measurement.NetworkLength
                + measurement.Destination.PartialLength;
            record.SourceGap = measurement.Source.Gap;
            record.DestinationGap = measurement.Destination.Gap;
            record.Bridge = measurement.Bridge;
            record.Total = measurement.Total;
            record.WithAllowance =
                measurement.Total * (1.0 + options.AllowancePercent / 100.0);
            record.VerticalDifference =
                measurement.Destination.EquipmentPoint.Z
                - measurement.Source.EquipmentPoint.Z;
            record.Points = measurement.Points;

            record.Current = options.Current;
            record.Voltage = options.Voltage;
            if (circuit != null)
            {
                record.CircuitNumber = circuit.CircuitNumber;
                record.LoadName = circuit.LoadName;
                if (circuit.Current > Geometry.Epsilon)
                {
                    record.Current = circuit.Current;
                    record.CurrentFromCircuit = true;
                }
                if (circuit.Voltage > Geometry.Epsilon)
                {
                    record.Voltage = circuit.Voltage;
                }
            }
            // The drop is worked out on the length including the allowance,
            // because that is the cable that gets installed.
            record.DropVolts = VoltageDrop.Volts(
                Units.FeetToMetres(record.WithAllowance),
                record.Current, options.MilliVoltsPerAmpMetre);
            record.DropPercent = record.Voltage > Geometry.Epsilon
                ? record.DropVolts / record.Voltage * 100.0
                : 0.0;
            record.WithinLimit =
                record.DropPercent <= options.LimitPercent + 1e-9;
            return record;
        }
    }

    /// <summary>One row of the panel summary.</summary>
    public class PanelSummary
    {
        public EquipmentItem Panel;
        public int RouteCount;
        public double Longest;
        public double WorstPercent;
        public int OverLimit;
        public int Bridged;

        public string Name
        {
            get { return Panel.Name; }
        }
    }

    /// <summary>Everything a measuring run produced.</summary>
    public class RouteResult
    {
        public List<RouteRecord> Routes = new List<RouteRecord>();
        public List<RouteProblem> Unreachable = new List<RouteProblem>();
        public List<EquipmentItem> Panels = new List<EquipmentItem>();
        public bool Cancelled;

        /// <summary>One row per panel, worst voltage drop first.</summary>
        public List<PanelSummary> PanelRows()
        {
            Dictionary<long, PanelSummary> rows =
                new Dictionary<long, PanelSummary>();
            for (int index = 0; index < Panels.Count; index++)
            {
                PanelSummary row = new PanelSummary();
                row.Panel = Panels[index];
                rows[Panels[index].Id] = row;
            }
            for (int index = 0; index < Routes.Count; index++)
            {
                RouteRecord route = Routes[index];
                long[] ends = new long[]
                {
                    route.Source.Id, route.Destination.Id
                };
                for (int end = 0; end < ends.Length; end++)
                {
                    PanelSummary row;
                    if (!rows.TryGetValue(ends[end], out row))
                    {
                        continue;
                    }
                    row.RouteCount++;
                    if (route.WithAllowance > row.Longest)
                    {
                        row.Longest = route.WithAllowance;
                    }
                    if (route.DropPercent > row.WorstPercent)
                    {
                        row.WorstPercent = route.DropPercent;
                    }
                    if (!route.WithinLimit)
                    {
                        row.OverLimit++;
                    }
                    if (route.Bridge > Geometry.Epsilon)
                    {
                        row.Bridged++;
                    }
                }
            }
            List<PanelSummary> ordered = new List<PanelSummary>(rows.Values);
            ordered.Sort(delegate (PanelSummary a, PanelSummary b)
            {
                if (a.RouteCount == 0 && b.RouteCount > 0)
                {
                    return 1;
                }
                if (b.RouteCount == 0 && a.RouteCount > 0)
                {
                    return -1;
                }
                int byDrop = b.WorstPercent.CompareTo(a.WorstPercent);
                if (byDrop != 0)
                {
                    return byDrop;
                }
                return string.Compare(a.Name, b.Name,
                    StringComparison.OrdinalIgnoreCase);
            });
            return ordered;
        }
    }
}
