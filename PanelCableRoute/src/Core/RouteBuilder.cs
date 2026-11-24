// -----------------------------------------------------------------------------
// Measuring every route the scope asks for.
//
// One Dijkstra pass per panel serves all of that panel's destinations, and the
// distance map is dropped before the next panel starts, so a building of P
// panels costs P passes and holds one map at a time, rather than one pass per
// pair.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;

namespace PanelCableRoute.Core
{
    /// <summary>What the model supplies for a measuring run.</summary>
    public class RouteInput
    {
        public TrayNetwork Network;
        public RouteOptions Options = new RouteOptions();
        /// <summary>Panels to measure from, in the order they are reported.</summary>
        public List<EquipmentItem> Panels = new List<EquipmentItem>();
        /// <summary>Every item a route can reach, by element id.</summary>
        public Dictionary<long, EquipmentItem> ItemsById =
            new Dictionary<long, EquipmentItem>();
        /// <summary>Isolators, by element id.</summary>
        public Dictionary<long, EquipmentItem> IsolatorsById =
            new Dictionary<long, EquipmentItem>();
        public List<CircuitLink> Circuits = new List<CircuitLink>();
        /// <summary>Which elements each element is wired to, by element id.</summary>
        public Dictionary<long, List<long>> Wiring =
            new Dictionary<long, List<long>>();
    }

    /// <summary>
    /// Called as each panel is measured. Return false to stop the run; what
    /// has been measured so far is kept and marked as cancelled.
    /// </summary>
    public delegate bool RouteProgress(int done, int total, string panelName);

    public static class RouteBuilder
    {
        public static RouteResult Build(RouteInput input, RouteProgress progress)
        {
            RouteResult result = new RouteResult();
            RouteOptions options = input.Options;
            TrayNetwork network = input.Network;

            Dictionary<long, List<Attachment>> attachments =
                new Dictionary<long, List<Attachment>>();

            // Circuits grouped by the panel that feeds them, and the couples
            // that are already a circuit so a pair does not repeat them.
            Dictionary<long, List<CircuitLink>> byBase =
                new Dictionary<long, List<CircuitLink>>();
            HashSet<string> circuitCouples = new HashSet<string>();
            for (int index = 0; index < input.Circuits.Count; index++)
            {
                CircuitLink circuit = input.Circuits[index];
                List<CircuitLink> list;
                if (!byBase.TryGetValue(circuit.BaseEquipmentId, out list))
                {
                    list = new List<CircuitLink>();
                    byBase[circuit.BaseEquipmentId] = list;
                }
                list.Add(circuit);
                circuitCouples.Add(CoupleKey(
                    circuit.BaseEquipmentId, circuit.MemberId));
            }

            List<EquipmentItem> inScope = new List<EquipmentItem>();
            for (int index = 0; index < input.Panels.Count; index++)
            {
                EquipmentItem panel = input.Panels[index];
                if (options.PanelIds.Count == 0
                        || options.PanelIds.Contains(panel.Id))
                {
                    inScope.Add(panel);
                }
            }
            result.Panels = inScope;

            HashSet<string> measured = new HashSet<string>();
            double maxPairFeet = options.MaxPairMetres > 0.0
                ? Units.MetresToFeet(options.MaxPairMetres)
                : 0.0;

            for (int index = 0; index < inScope.Count; index++)
            {
                EquipmentItem panel = inScope[index];
                if (progress != null
                        && !progress(index + 1, inScope.Count, panel.Name))
                {
                    result.Cancelled = true;
                    break;
                }
                List<Attachment> sourceAttachments =
                    AttachmentsFor(network, attachments, panel);
                if (sourceAttachments.Count == 0)
                {
                    AddProblem(result, panel, "no cable tray to attach to");
                    continue;
                }
                Reach reach = network.DistancesFrom(sourceAttachments);

                // --- the equipment on this panel's circuits -----------------
                List<long> circuitMembers = new List<long>();
                List<CircuitLink> circuits;
                if (byBase.TryGetValue(panel.Id, out circuits))
                {
                    for (int item = 0; item < circuits.Count; item++)
                    {
                        CircuitLink circuit = circuits[item];
                        circuitMembers.Add(circuit.MemberId);
                        if (!options.MeasureCircuits)
                        {
                            continue;
                        }
                        EquipmentItem member;
                        if (!input.ItemsById.TryGetValue(
                                circuit.MemberId, out member))
                        {
                            continue;
                        }
                        if (!options.IncludeIsolators && member.IsIsolator)
                        {
                            continue;
                        }
                        string key = CoupleKey(panel.Id, member.Id) + "|c";
                        if (!measured.Add(key))
                        {
                            continue;
                        }
                        MeasureInto(result, network, reach, sourceAttachments,
                            attachments, RouteKind.Circuit, panel, member,
                            options, circuit, 0.0);
                    }
                }

                // --- isolators wired behind that equipment ------------------
                if (options.IncludeIsolators && input.IsolatorsById.Count > 0)
                {
                    List<long> behind = IsolatorsBehind(
                        circuitMembers, input.IsolatorsById, input.Wiring);
                    for (int item = 0; item < behind.Count; item++)
                    {
                        EquipmentItem isolator = input.IsolatorsById[behind[item]];
                        string key = CoupleKey(panel.Id, isolator.Id) + "|i";
                        if (!measured.Add(key))
                        {
                            continue;
                        }
                        MeasureInto(result, network, reach, sourceAttachments,
                            attachments, RouteKind.Isolator, panel, isolator,
                            options, null, 0.0);
                    }
                }

                // --- panel to panel ----------------------------------------
                if (options.MeasurePanelPairs)
                {
                    for (int other = index + 1; other < inScope.Count; other++)
                    {
                        EquipmentItem partner = inScope[other];
                        if (circuitCouples.Contains(
                                CoupleKey(panel.Id, partner.Id)))
                        {
                            continue;   // already measured as a circuit
                        }
                        string key = CoupleKey(panel.Id, partner.Id) + "|p";
                        if (!measured.Add(key))
                        {
                            continue;
                        }
                        MeasureInto(result, network, reach, sourceAttachments,
                            attachments, RouteKind.PanelPair, panel, partner,
                            options, null, maxPairFeet);
                    }
                }
                reach = null;       // free the distance map before the next panel
            }

            result.Routes.Sort(delegate (RouteRecord a, RouteRecord b)
            {
                int byDrop = b.DropPercent.CompareTo(a.DropPercent);
                if (byDrop != 0)
                {
                    return byDrop;
                }
                return b.Total.CompareTo(a.Total);
            });
            return result;
        }

        private static void MeasureInto(
            RouteResult result, TrayNetwork network, Reach reach,
            List<Attachment> sourceAttachments,
            Dictionary<long, List<Attachment>> attachments,
            RouteKind kind, EquipmentItem source, EquipmentItem destination,
            RouteOptions options, CircuitLink circuit, double maxTotalFeet)
        {
            List<Attachment> destinationAttachments =
                AttachmentsFor(network, attachments, destination);
            if (destinationAttachments.Count == 0)
            {
                if (kind != RouteKind.PanelPair)
                {
                    AddProblem(result, destination, "no cable tray to attach to");
                }
                return;
            }
            RouteMeasurement measurement = Router.Measure(
                network, reach, sourceAttachments, destinationAttachments);
            if (measurement == null)
            {
                if (kind != RouteKind.PanelPair)
                {
                    AddProblem(result, destination, "no route through the trays");
                }
                return;
            }
            if (maxTotalFeet > 0.0 && measurement.Total > maxTotalFeet)
            {
                return;
            }
            result.Routes.Add(RouteRecord.Create(
                kind, source, destination, measurement, options, circuit));
        }

        private static List<Attachment> AttachmentsFor(
            TrayNetwork network,
            Dictionary<long, List<Attachment>> cache, EquipmentItem item)
        {
            List<Attachment> found;
            if (cache.TryGetValue(item.Id, out found))
            {
                return found;
            }
            found = item.Points.Count > 0
                ? network.Attach(item.Points)
                : new List<Attachment>();
            cache[item.Id] = found;
            return found;
        }

        private static void AddProblem(
            RouteResult result, EquipmentItem item, string reason)
        {
            for (int index = 0; index < result.Unreachable.Count; index++)
            {
                if (result.Unreachable[index].Item.Id == item.Id)
                {
                    return;         // report each item once
                }
            }
            RouteProblem problem = new RouteProblem();
            problem.Item = item;
            problem.Reason = reason;
            result.Unreachable.Add(problem);
        }

        private static string CoupleKey(long first, long second)
        {
            long low = Math.Min(first, second);
            long high = Math.Max(first, second);
            return low.ToString() + ":" + high.ToString();
        }

        /// <summary>
        /// Isolators wired to the given elements, following the connectors on.
        /// An isolator usually sits between the panel and the plant, so it is
        /// reached through the equipment rather than from the panel's circuit.
        /// </summary>
        public static List<long> IsolatorsBehind(
            IList<long> startIds,
            Dictionary<long, EquipmentItem> isolatorsById,
            Dictionary<long, List<long>> wiring)
        {
            List<long> found = new List<long>();
            HashSet<long> seen = new HashSet<long>();
            HashSet<long> visited = new HashSet<long>();
            Stack<long> pending = new Stack<long>();
            for (int index = 0; index < startIds.Count; index++)
            {
                pending.Push(startIds[index]);
            }
            while (pending.Count > 0)
            {
                long current = pending.Pop();
                if (!visited.Add(current))
                {
                    continue;
                }
                List<long> neighbours;
                if (!wiring.TryGetValue(current, out neighbours))
                {
                    continue;
                }
                for (int index = 0; index < neighbours.Count; index++)
                {
                    long neighbour = neighbours[index];
                    if (isolatorsById.ContainsKey(neighbour) && seen.Add(neighbour))
                    {
                        found.Add(neighbour);
                        pending.Push(neighbour);
                    }
                }
            }
            found.Sort();
            return found;
        }
    }
}
