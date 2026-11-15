// -----------------------------------------------------------------------------
// Checks for the routing core. No Revit, no test framework: it builds and runs
// with the plain SDK, and returns a non-zero exit code when something fails.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using System.Globalization;
using PanelCableRoute.Core;

namespace PanelCableRoute.CoreTests
{
    public static class Program
    {
        private static int _failures;

        public static int Main(string[] args)
        {
            LShapedRun();
            DisconnectedTrays();
            ToleranceClosesTheGap();
            ShortestWayRoundALoop();
            RiserPreferredOffLevel();
            FittingBridgesTrayEnds();
            VoltageDropMaths();
            ExportTable();
            PanelSummaryRows();
            TubeGeometry();
            BuilderEndToEnd();
            IsolatorsBehindEquipment();
            BridgingBrokenTrayRuns();
            TrayPreferenceBeatsAShortHop();

            Console.WriteLine();
            if (_failures == 0)
            {
                Console.WriteLine("All core checks passed.");
                return 0;
            }
            Console.WriteLine(_failures.ToString() + " check(s) FAILED.");
            return 1;
        }

        // ----- helpers -------------------------------------------------------
        private static double M(double metres)
        {
            return Units.MetresToFeet(metres);
        }

        private static Pt P(double x, double y, double z)
        {
            return new Pt(M(x), M(y), M(z));
        }

        private static double Metres(double feet)
        {
            return Units.FeetToMetres(feet);
        }

        private static void Head(string title)
        {
            Console.WriteLine();
            Console.WriteLine("=== " + title);
        }

        private static void Near(string what, double actual, double expected)
        {
            bool ok = Math.Abs(actual - expected) < 0.01;
            if (!ok)
            {
                _failures++;
            }
            Console.WriteLine(string.Format(CultureInfo.InvariantCulture,
                "   [{0}] {1}: {2:0.000} (expected {3:0.000})",
                ok ? "PASS" : "FAIL", what, actual, expected));
        }

        private static void True(string what, bool actual)
        {
            if (!actual)
            {
                _failures++;
            }
            Console.WriteLine("   [" + (actual ? "PASS" : "FAIL") + "] " + what);
        }

        private static void Same(string what, string actual, string expected)
        {
            bool ok = string.Equals(actual, expected, StringComparison.Ordinal);
            if (!ok)
            {
                _failures++;
            }
            Console.WriteLine("   [" + (ok ? "PASS" : "FAIL") + "] " + what
                + ": \"" + actual + "\""
                + (ok ? "" : " (expected \"" + expected + "\")"));
        }

        private static TraySegment Tray(long id, Pt start, Pt end)
        {
            TraySegment tray = new TraySegment();
            tray.Id = id;
            tray.Start = start;
            tray.End = end;
            tray.WidthMm = 300.0;
            tray.HeightMm = 100.0;
            return tray;
        }

        private static TrayNetwork Network(
            List<TraySegment> trays, double toleranceMm)
        {
            return new TrayNetwork(trays, new List<FittingLink>(), toleranceMm);
        }

        private static EquipmentItem Item(long id, string name, Pt point)
        {
            EquipmentItem item = new EquipmentItem();
            item.Id = id;
            item.Name = name;
            item.LevelName = "Level 1";
            item.Points.Add(point);
            return item;
        }

        private static RouteOptions Options()
        {
            return new RouteOptions();
        }

        private static RouteMeasurement Between(
            TrayNetwork network, Pt from, Pt to)
        {
            List<Attachment> source = network.Attach(new List<Pt> { from });
            List<Attachment> destination = network.Attach(new List<Pt> { to });
            Reach reach = network.DistancesFrom(source);
            return Router.Measure(network, reach, source, destination);
        }

        // ----- the checks ----------------------------------------------------
        private static void LShapedRun()
        {
            Head("L-shaped tray run, a panel 1 m off each end");
            // (0,0)->(10,0)->(10,8) metres; panels at (0,-1) and (10,9).
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(10, 0, 0), P(10, 8, 0))
            }, 100.0);
            True("the two trays share one node (3 nodes, not 4)",
                network.NodeCount == 3);
            RouteMeasurement route = Between(network, P(0, -1, 0), P(10, 9, 0));
            True("a route was found", route != null);
            if (route == null)
            {
                return;
            }
            double tray = route.Source.PartialLength + route.NetworkLength
                + route.Destination.PartialLength;
            Near("along the trays (m)", Metres(tray), 18.0);
            Near("the two end connections (m)",
                Metres(route.Source.Gap + route.Destination.Gap), 2.0);
            Near("total (m)", Metres(route.Total), 20.0);
            Near("bridged gap (m)", Metres(route.Bridge), 0.0);
            True("the drawn route follows the trays (more than 4 points)",
                route.Points.Count > 4);
            True("one tray edge was traversed", route.PathEdges.Count == 1);
        }

        private static void DisconnectedTrays()
        {
            Head("Trays with a 2 m break in them");
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(12, 0, 0), P(22, 0, 0))
            }, 100.0);
            RouteMeasurement route = Between(network, P(0, -1, 0), P(22, -1, 0));
            True("a route was still measured", route != null);
            if (route == null)
            {
                return;
            }
            Near("total (m)", Metres(route.Total), 24.0);
            Near("the bridge is the real break, not a diagonal (m)",
                Metres(route.Bridge), 2.0);
            Near("nothing was counted as network length (m)",
                Metres(route.NetworkLength), 0.0);
        }

        private static void ToleranceClosesTheGap()
        {
            Head("The same break, with the tolerance raised to 2.5 m");
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(12, 0, 0), P(22, 0, 0))
            }, 2500.0);
            True("the two ends now share a node", network.NodeCount == 3);
            RouteMeasurement route = Between(network, P(0, -1, 0), P(22, -1, 0));
            Near("total (m)", Metres(route.Total), 22.0);
            Near("no bridge is needed (m)", Metres(route.Bridge), 0.0);
        }

        private static void ShortestWayRoundALoop()
        {
            Head("A loop of trays - the shorter way must win");
            // A 10 x 4 m ring. From (0,-1) to (6,5): the short way is up the
            // left side and along the top.
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(10, 0, 0), P(10, 4, 0)),
                Tray(3, P(0, 0, 0), P(0, 4, 0)),
                Tray(4, P(0, 4, 0), P(10, 4, 0))
            }, 100.0);
            RouteMeasurement route = Between(network, P(0, -1, 0), P(6, 5, 0));
            True("a route was found", route != null);
            // Left side (4 m) + along the top to x=6 (6 m) = 10 m of tray,
            // plus 1 m at each end. The long way round is 16 m of tray.
            Near("total the short way round (m)", Metres(route.Total), 12.0);
            Near("tray length the short way (m)",
                Metres(route.Source.PartialLength + route.NetworkLength
                    + route.Destination.PartialLength), 10.0);
            True("nothing was bridged", route.Bridge < 0.001);
            // Both ends attach to the corner node, so the route is carried by
            // the two tray legs and needs no graph edge. What matters is that
            // the drawn line turns the corner instead of cutting across.
            bool turnsTheCorner = false;
            for (int index = 0; index < route.Points.Count; index++)
            {
                if (route.Points[index].DistanceTo(P(0, 4, 0))
                        < Units.MmToFeet(1.0))
                {
                    turnsTheCorner = true;
                }
            }
            True("the drawn route turns the corner of the loop",
                turnsTheCorner);
        }

        private static void RiserPreferredOffLevel()
        {
            Head("Equipment a floor below, with a riser to reach it");
            // Tray at +4 m, a riser down to the floor, equipment 2 m from the
            // foot of the riser with nothing else on its level.
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 4), P(20, 0, 4)),
                Tray(2, P(6, 0, 0), P(6, 0, 4))
            }, 100.0);
            List<Attachment> attachments =
                network.Attach(new List<Pt> { P(8, 0, 0) });
            True("attachments were found", attachments.Count > 0);
            Attachment best = attachments[0];
            True("it attaches to the riser, not the tray overhead",
                best.TrayId == 2);
            True("the riser is recognised as vertical", best.Vertical);
            Near("the drop onto the riser (m)", Metres(best.Gap), 2.0);
        }

        private static void FittingBridgesTrayEnds()
        {
            Head("A fitting joins two tray ends that do not touch");
            // Two trays 300 mm apart with a bend modelled between them: the
            // tolerance alone would not join them.
            List<TraySegment> trays = new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(10.3, 0, 0), P(20.3, 0, 0))
            };
            FittingLink fitting = new FittingLink();
            fitting.Id = 99;
            fitting.ConnectorPoints.Add(P(10, 0, 0));
            fitting.ConnectorPoints.Add(P(10.3, 0, 0));
            TrayNetwork network = new TrayNetwork(
                trays, new List<FittingLink> { fitting }, 100.0);
            True("the fitting was bridged", network.FittingsBridged == 1);
            RouteMeasurement route = Between(network, P(0, -1, 0), P(20.3, -1, 0));
            Near("total through the fitting (m)", Metres(route.Total), 22.3);
            Near("no bridge was needed (m)", Metres(route.Bridge), 0.0);
        }

        private static void VoltageDropMaths()
        {
            Head("Voltage drop");
            Near("100 m at 32 A on 1.1 mV/A/m (V)",
                VoltageDrop.Volts(100.0, 32.0, 1.1), 3.52);
            Near("the 5 % limit at 32 A is reached at (m)",
                VoltageDrop.ReachMetres(400.0, 5.0, 32.0, 1.1), 568.182);

            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(100, 0, 0))
            }, 100.0);
            RouteMeasurement measurement =
                Between(network, P(0, -1, 0), P(100, -1, 0));
            RouteOptions options = Options();
            CircuitLink circuit = new CircuitLink();
            circuit.CircuitNumber = "L1/3";
            circuit.LoadName = "AHU-01";
            circuit.Current = 63.0;
            circuit.Voltage = 400.0;
            RouteRecord record = RouteRecord.Create(
                RouteKind.Circuit, Item(1, "DB-01", P(0, -1, 0)),
                Item(2, "AHU-01", P(100, -1, 0)), measurement, options,
                circuit);
            Near("route length (m)", Metres(record.Total), 102.0);
            Near("with the 5 % allowance (m)",
                Metres(record.WithAllowance), 107.1);
            True("the circuit's current overrides the dialog",
                record.CurrentFromCircuit && Math.Abs(record.Current - 63.0) < 0.001);
            Near("drop (V)", record.DropVolts,
                VoltageDrop.Volts(107.1, 63.0, 1.1));
            Near("drop (%)", record.DropPercent, record.DropVolts / 4.0);
            True("within the 5 % limit", record.WithinLimit);

            RouteOptions tight = Options();
            tight.LimitPercent = 1.0;
            RouteRecord flagged = RouteRecord.Create(
                RouteKind.Circuit, Item(1, "DB-01", P(0, -1, 0)),
                Item(2, "AHU-01", P(100, -1, 0)), measurement, tight, circuit);
            True("over a 1 % limit it is flagged", !flagged.WithinLimit);

            RouteRecord noCircuit = RouteRecord.Create(
                RouteKind.PanelPair, Item(1, "DB-01", P(0, -1, 0)),
                Item(3, "DB-02", P(100, -1, 0)), measurement, options, null);
            True("without a circuit the dialog's current is used",
                !noCircuit.CurrentFromCircuit
                && Math.Abs(noCircuit.Current - options.Current) < 0.001);
        }

        private static void ExportTable()
        {
            Head("Export table and CSV escaping");
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0))
            }, 100.0);
            RouteMeasurement measurement =
                Between(network, P(0, -1, 0), P(10, -1, 0));
            RouteOptions options = Options();
            RouteRecord record = RouteRecord.Create(
                RouteKind.PanelPair, Item(1, "DB-01", P(0, -1, 0)),
                Item(2, "Panel \"A\", main", P(10, -1, 0)), measurement,
                options, null);
            List<string[]> rows = Export.Rows(
                new List<RouteRecord> { record }, options);
            True("one row per route", rows.Count == 1);
            True("every column is filled in",
                rows[0].Length == Export.Header.Length);
            Same("a name with a comma and quotes is escaped",
                Export.CsvField("Panel \"A\", main"),
                "\"Panel \"\"A\"\", main\"");
            string csv = Export.Csv(rows);
            True("the CSV has a header and one row",
                csv.Split(new string[] { "\r\n" }, StringSplitOptions.RemoveEmptyEntries)
                    .Length == 2);
            Same("the status column", rows[0][21], "OK");
            string tabbed = Export.Tabbed(rows);
            True("the tabbed form is not quoted",
                tabbed.IndexOf("\"\"", StringComparison.Ordinal) < 0);
        }

        private static void PanelSummaryRows()
        {
            Head("Panel summary");
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(200, 0, 0))
            }, 100.0);
            RouteOptions options = Options();
            options.LimitPercent = 1.0;
            EquipmentItem first = Item(1, "DB-01", P(0, -1, 0));
            EquipmentItem second = Item(2, "DB-02", P(60, -1, 0));
            EquipmentItem third = Item(3, "DB-03", P(190, -1, 0));
            EquipmentItem spare = Item(4, "DB-04-spare", P(0, -1, 0));
            RouteResult result = new RouteResult();
            result.Panels.AddRange(new EquipmentItem[]
            {
                first, second, third, spare
            });
            result.Routes.Add(RouteRecord.Create(RouteKind.PanelPair, first,
                second, Between(network, P(0, -1, 0), P(60, -1, 0)), options,
                null));
            result.Routes.Add(RouteRecord.Create(RouteKind.PanelPair, first,
                third, Between(network, P(0, -1, 0), P(190, -1, 0)), options,
                null));
            List<PanelSummary> rows = result.PanelRows();
            True("a row for every panel", rows.Count == 4);
            Same("the worst panel comes first", rows[0].Name, "DB-01");
            True("DB-01 is on both routes", rows[0].RouteCount == 2);
            True("its worst route is over the 1 % limit",
                rows[0].OverLimit == 1);
            Same("a panel with no routes comes last",
                rows[rows.Count - 1].Name, "DB-04-spare");
            True("and has no routes", rows[rows.Count - 1].RouteCount == 0);
        }

        private static void TubeGeometry()
        {
            Head("Geometry for the 3D preview");
            Pt[] tube = Geometry.TubeCorners(
                new Pt(0, 0, 0), new Pt(10, 0, 0), 0.5, 0.5);
            True("a tube has eight corners", tube != null && tube.Length == 8);
            double spanY = 0.0;
            double spanZ = 0.0;
            double lowY = double.MaxValue;
            double highY = double.MinValue;
            double lowZ = double.MaxValue;
            double highZ = double.MinValue;
            for (int index = 0; index < tube.Length; index++)
            {
                lowY = Math.Min(lowY, tube[index].Y);
                highY = Math.Max(highY, tube[index].Y);
                lowZ = Math.Min(lowZ, tube[index].Z);
                highZ = Math.Max(highZ, tube[index].Z);
            }
            spanY = highY - lowY;
            spanZ = highZ - lowZ;
            Near("its width", spanY, 1.0);
            Near("its height", spanZ, 1.0);
            True("a zero-length tube is refused", Geometry.TubeCorners(
                new Pt(0, 0, 0), new Pt(0, 0, 0), 0.5, 0.5) == null);

            Pt[] box = Geometry.BoxCorners(new Pt(1, 2, 3), new Pt(4, 6, 9));
            double boxLowX = double.MaxValue;
            double boxHighZ = double.MinValue;
            for (int index = 0; index < box.Length; index++)
            {
                boxLowX = Math.Min(boxLowX, box[index].X);
                boxHighZ = Math.Max(boxHighZ, box[index].Z);
            }
            Near("a box keeps its low corner", boxLowX, 1.0);
            Near("a box keeps its high corner", boxHighZ, 9.0);

            // A tray at 45 degrees in plan keeps its true cross-section.
            Pt normal;
            Pt up;
            bool made = Geometry.TraySection(
                new Pt(0, 0, 0), new Pt(10, 10, 0),
                Units.MmToFeet(150.0), Units.MmToFeet(50.0),
                out normal, out up);
            True("a 45 degree tray has a section", made);
            Near("half width stays 150 mm", Units.FeetToMm(normal.Length), 150.0);
            Near("half height stays 50 mm", Units.FeetToMm(up.Length), 50.0);
            Near("and the height is vertical", Math.Abs(up.Z),
                Units.MmToFeet(50.0));
        }

        private static void BuilderEndToEnd()
        {
            Head("The builder: circuits and panel pairs together");
            TrayNetwork network = Network(new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(100, 0, 0))
            }, 100.0);
            EquipmentItem main = Item(1, "MSB", P(0, -1, 0));
            EquipmentItem board = Item(2, "DB-01", P(50, -1, 0));
            EquipmentItem plant = Item(3, "AHU-01", P(90, -1, 0));
            EquipmentItem isolator = Item(4, "ISO-AHU-01", P(95, -1, 0));
            isolator.IsIsolator = true;

            RouteInput input = new RouteInput();
            input.Network = network;
            input.Options = Options();
            input.Panels.Add(main);
            input.Panels.Add(board);
            input.ItemsById[main.Id] = main;
            input.ItemsById[board.Id] = board;
            input.ItemsById[plant.Id] = plant;
            input.ItemsById[isolator.Id] = isolator;
            input.IsolatorsById[isolator.Id] = isolator;
            // MSB feeds DB-01; DB-01 feeds the AHU; the isolator is wired to
            // the AHU rather than onto a circuit of its own.
            CircuitLink feeder = new CircuitLink();
            feeder.BaseEquipmentId = main.Id;
            feeder.MemberId = board.Id;
            feeder.CircuitNumber = "MSB/1";
            feeder.Current = 100.0;
            feeder.Voltage = 400.0;
            CircuitLink final = new CircuitLink();
            final.BaseEquipmentId = board.Id;
            final.MemberId = plant.Id;
            final.CircuitNumber = "DB-01/4";
            final.Current = 20.0;
            final.Voltage = 400.0;
            input.Circuits.Add(feeder);
            input.Circuits.Add(final);
            input.Wiring[plant.Id] = new List<long> { isolator.Id };
            input.Wiring[isolator.Id] = new List<long> { plant.Id };

            RouteResult result = RouteBuilder.Build(input, null);
            True("nothing was cancelled", !result.Cancelled);
            int circuits = 0;
            int isolators = 0;
            int pairs = 0;
            for (int index = 0; index < result.Routes.Count; index++)
            {
                RouteKind kind = result.Routes[index].Kind;
                if (kind == RouteKind.Circuit)
                {
                    circuits++;
                }
                else if (kind == RouteKind.Isolator)
                {
                    isolators++;
                }
                else
                {
                    pairs++;
                }
            }
            True("both circuits were measured", circuits == 2);
            True("the isolator behind the AHU was found", isolators == 1);
            True("the MSB-to-DB-01 pair was not repeated as a pair",
                pairs == 0);
            True("every route has points to draw",
                result.Routes.Count > 0
                && result.Routes[0].Points.Count > 0);

            // Without the circuits, the pair is measured instead.
            RouteInput pairsOnly = new RouteInput();
            pairsOnly.Network = network;
            pairsOnly.Options = Options();
            pairsOnly.Options.MeasureCircuits = false;
            pairsOnly.Options.IncludeIsolators = false;
            pairsOnly.Panels.Add(main);
            pairsOnly.Panels.Add(board);
            pairsOnly.ItemsById[main.Id] = main;
            pairsOnly.ItemsById[board.Id] = board;
            RouteResult second = RouteBuilder.Build(pairsOnly, null);
            True("one pair for two panels", second.Routes.Count == 1);
            Near("MSB to DB-01 (m)", Metres(second.Routes[0].Total), 52.0);

            // A pair length limit drops the long ones.
            pairsOnly.Options.MaxPairMetres = 10.0;
            RouteResult limited = RouteBuilder.Build(pairsOnly, null);
            True("the 52 m pair is dropped by a 10 m limit",
                limited.Routes.Count == 0);

            // Scope: only the panels asked for.
            pairsOnly.Options.MaxPairMetres = 0.0;
            pairsOnly.Options.PanelIds.Add(main.Id);
            RouteResult scoped = RouteBuilder.Build(pairsOnly, null);
            True("one panel in scope gives no pair",
                scoped.Panels.Count == 1 && scoped.Routes.Count == 0);

            // Equipment nowhere near a tray is reported, not silently dropped.
            RouteInput stranded = new RouteInput();
            stranded.Network = network;
            stranded.Options = Options();
            stranded.Options.MeasurePanelPairs = false;
            EquipmentItem nowhere = Item(5, "DB-NOWHERE", P(0, 0, 0));
            nowhere.Points.Clear();     // no connectors, no location
            stranded.Panels.Add(nowhere);
            stranded.ItemsById[nowhere.Id] = nowhere;
            RouteResult strandedResult = RouteBuilder.Build(stranded, null);
            True("the stranded panel is reported",
                strandedResult.Unreachable.Count == 1);
            Same("with a reason", strandedResult.Unreachable[0].Reason,
                "no cable tray to attach to");

            // The progress callback can stop the run.
            RouteInput cancelled = new RouteInput();
            cancelled.Network = network;
            cancelled.Options = Options();
            cancelled.Panels.Add(main);
            cancelled.Panels.Add(board);
            cancelled.ItemsById[main.Id] = main;
            cancelled.ItemsById[board.Id] = board;
            RouteResult stopped = RouteBuilder.Build(cancelled,
                delegate (int done, int total, string name) { return false; });
            True("cancelling is recorded", stopped.Cancelled);
            True("and nothing was measured", stopped.Routes.Count == 0);
        }

        private static void BridgingBrokenTrayRuns()
        {
            Head("Tray runs that stop short of each other");
            // Three runs in a line with 1 m gaps: the model is in pieces, as
            // a real one usually is where the trays were not joined up.
            List<TraySegment> trays = new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(11, 0, 0), P(21, 0, 0)),
                Tray(3, P(22, 0, 0), P(32, 0, 0))
            };
            TrayNetwork loose = Network(trays, 100.0);
            int before;
            loose.BridgeGaps(0.0, 8.0);         // 0 = do not bridge anything
            before = loose.ComponentCount;
            True("without bridging there are three separate groups",
                before == 3);

            TrayNetwork joined = Network(trays, 100.0);
            joined.BridgeGaps(3000.0, 8.0);
            True("bridging joins them into one", joined.ComponentCount == 1);
            True("two hops were added", joined.BridgeEdgeCount == 2);
            Near("and they total the real gap (m)",
                Metres(joined.BridgeLengthAdded), 2.0);

            RouteMeasurement route = Between(
                joined, P(0, -1, 0), P(32, -1, 0));
            True("a route was found", route != null);
            Near("total (m)", Metres(route.Total), 34.0);
            Near("of which along the trays (m)",
                Metres(route.Source.PartialLength + route.NetworkLength
                    + route.Destination.PartialLength), 30.0);
            Near("and hopped (m)", Metres(route.Bridge), 2.0);
            True("the route runs through the tray edges, not across open "
                + "space", route.PathEdges.Count >= 4);
            int trayEdges = 0;
            int bridgeEdges = 0;
            for (int index = 0; index < route.PathEdges.Count; index++)
            {
                if (route.PathEdges[index].Kind == EdgeKind.Bridge)
                {
                    bridgeEdges++;
                }
                else
                {
                    trayEdges++;
                }
            }
            // The first run is covered by the source's own leg along it
            // rather than by a graph edge, so two or three edges are both
            // right; what matters is that the trays carry the route.
            True("the tray runs were followed", trayEdges >= 2);
            True("with two hops between them", bridgeEdges == 2);
        }

        private static void TrayPreferenceBeatsAShortHop()
        {
            Head("A tray way round against a short hop through the air");
            // Two runs in line with a 2 m gap, and a short tray alongside
            // that nearly touches both of them (200 mm at each end). The
            // route can either hop the 2 m gap in one go, or take two tiny
            // hops and 2 m of tray.
            List<TraySegment> trays = new List<TraySegment>
            {
                Tray(1, P(0, 0, 0), P(10, 0, 0)),
                Tray(2, P(12, 0, 0), P(22, 0, 0)),
                Tray(3, P(10, 0.2, 0), P(12, 0.2, 0))
            };

            // Raw length only: the single 2 m hop is the shortest way.
            TrayNetwork plain = Network(trays, 100.0);
            plain.BridgeGaps(3000.0, 1.0);
            RouteMeasurement direct = Between(plain, P(0, -1, 0), P(22, -1, 0));
            Near("no preference: it hops the 2 m gap in one go (m)",
                Metres(direct.Bridge), 2.0);
            Near("and stays on 20 m of tray (m)",
                Metres(direct.Source.PartialLength + direct.NetworkLength
                    + direct.Destination.PartialLength), 20.0);

            // Preferring trays: two 200 mm hops and the short tray cost less
            // than one 2 m hop, even though the route is longer.
            TrayNetwork preferred = Network(trays, 100.0);
            preferred.BridgeGaps(3000.0, 8.0);
            RouteMeasurement around = Between(
                preferred, P(0, -1, 0), P(22, -1, 0));
            Near("preferring trays: only the two short hops (m)",
                Metres(around.Bridge), 0.4);
            Near("and 22 m of tray, the extra run included (m)",
                Metres(around.Source.PartialLength + around.NetworkLength
                    + around.Destination.PartialLength), 22.0);
            Near("the reported total is the real length, not the weighted "
                + "one (m)", Metres(around.Total), 24.4);
            True("it is the longer route, which is the point of preferring "
                + "trays", around.Total > direct.Total);
        }

        private static void IsolatorsBehindEquipment()
        {
            Head("Following the wiring to isolators behind the equipment");
            Dictionary<long, EquipmentItem> isolators =
                new Dictionary<long, EquipmentItem>();
            EquipmentItem first = Item(10, "ISO-1", new Pt(0, 0, 0));
            EquipmentItem second = Item(11, "ISO-2", new Pt(0, 0, 0));
            isolators[first.Id] = first;
            isolators[second.Id] = second;
            Dictionary<long, List<long>> wiring =
                new Dictionary<long, List<long>>();
            wiring[1] = new List<long> { 10 };          // plant -> ISO-1
            wiring[10] = new List<long> { 1, 11 };      // ISO-1 -> ISO-2
            wiring[11] = new List<long> { 10 };
            List<long> found = RouteBuilder.IsolatorsBehind(
                new List<long> { 1 }, isolators, wiring);
            True("both isolators in the chain are found", found.Count == 2);
            True("in a stable order", found[0] == 10 && found[1] == 11);

            // A loop in the wiring must not hang.
            wiring[1].Add(11);
            List<long> again = RouteBuilder.IsolatorsBehind(
                new List<long> { 1 }, isolators, wiring);
            True("a loop in the wiring is handled", again.Count == 2);
        }
    }
}
