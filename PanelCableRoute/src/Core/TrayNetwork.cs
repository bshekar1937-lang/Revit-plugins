// -----------------------------------------------------------------------------
// The cable tray network: every modelled tray centre line joined into one
// graph, with the shortest-path work the routes are measured with.
//
// Nodes are tray ends. Two ends within the connection tolerance share a node,
// which is how a run of trays becomes a connected path. Tray fittings bridge
// the ends plugged into them.
//
// Equipment is attached to the network at the point on the nearest tray, then
// along that tray to whichever end the route needs. Both ends are offered,
// because which one is cheaper depends on where the route goes next.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;

namespace PanelCableRoute.Core
{
    /// <summary>A cell of the spatial hash used to find nodes and trays.</summary>
    internal struct Cell : IEquatable<Cell>
    {
        public readonly int X;
        public readonly int Y;
        public readonly int Z;

        public Cell(int x, int y, int z)
        {
            X = x;
            Y = y;
            Z = z;
        }

        public bool Equals(Cell other)
        {
            return X == other.X && Y == other.Y && Z == other.Z;
        }

        public override bool Equals(object other)
        {
            return other is Cell && Equals((Cell)other);
        }

        public override int GetHashCode()
        {
            unchecked
            {
                int hash = 17;
                hash = hash * 31 + X;
                hash = hash * 31 + Y;
                hash = hash * 31 + Z;
                return hash;
            }
        }
    }

    public enum EdgeKind
    {
        Tray,
        Fitting,
        /// <summary>A hop through the air between two unjoined tray ends.</summary>
        Bridge
    }

    /// <summary>One hop of the network, and the points it is drawn with.</summary>
    public class NetworkEdge
    {
        public EdgeKind Kind;
        public long OwnerId;        // the tray or the fitting
        public int FromNode;        // the node Points starts at
        public int ToNode;
        /// <summary>What the router pays. A bridge costs more than its length.</summary>
        public double Weight;
        /// <summary>The real distance, which is what gets reported.</summary>
        public double RealLength;
        public Pt[] Points;         // centre line, FromNode to ToNode
    }

    /// <summary>Where a piece of equipment joins the network.</summary>
    public class Attachment
    {
        /// <summary>The network node this attachment leads to.</summary>
        public int Node;
        /// <summary>Orthogonal run from the equipment to the tray.</summary>
        public double Gap;
        /// <summary>Distance along the tray from the drop point to that node.</summary>
        public double PartialLength;
        public Pt EquipmentPoint;
        public Pt TrayPoint;
        public long TrayId;
        public bool Vertical;
        /// <summary>The tray centre line from the drop point to the node.</summary>
        public Pt[] PartialPoints;
        /// <summary>
        /// What the drop off the tray is multiplied by when the router
        /// compares routes. Travel along a tray is never weighted.
        /// </summary>
        public double GapWeight = 1.0;

        /// <summary>The real distance, which is what gets reported.</summary>
        public double Cost
        {
            get { return Gap + PartialLength; }
        }

        /// <summary>
        /// What the router pays. Dropping off a tray is off-tray travel, so
        /// it is weighted exactly as a hop between tray runs is: without
        /// that, a route will happily fly several metres through the air to
        /// a tray that saves it a short walk.
        /// </summary>
        public double WeightedCost
        {
            get { return Gap * GapWeight + PartialLength; }
        }
    }

    /// <summary>The result of one shortest-path pass out of a panel.</summary>
    public class Reach
    {
        public Dictionary<int, double> Distances = new Dictionary<int, double>();
        public Dictionary<int, NetworkEdge> Previous = new Dictionary<int, NetworkEdge>();
        /// <summary>Which attachment node each node's cheapest path started from.</summary>
        public Dictionary<int, int> Seeds = new Dictionary<int, int>();
    }

    public class TrayNetwork
    {
        /// <summary>Attachment candidates kept per item, cheapest first.</summary>
        public const int MaxAttachments = 8;

        /// <summary>
        /// A cable leaves the tray at the point nearest the equipment.
        /// Candidates whose gap is more than this beyond the nearest one are
        /// dropped: without that, projecting onto every tray in the building
        /// invents an attachment at every node whose cost merely repeats the
        /// travel along the trays, which hides the real tray route and
        /// mis-states the connection lengths.
        /// </summary>
        public const double AttachGapSlackMm = 2000.0;

        /// <summary>Routes within this are the same length as far as anyone cares.</summary>
        public const double TieToleranceMm = 1.0;

        private readonly double _tolerance;
        private readonly double _cellSize;
        /// <summary>Set by BridgeGaps; 1.0 means off-tray travel is not weighted.</summary>
        private double _preference = 1.0;
        private readonly List<Pt> _nodePoints = new List<Pt>();
        private readonly Dictionary<Cell, List<int>> _nodeCells =
            new Dictionary<Cell, List<int>>();
        private readonly Dictionary<int, List<NetworkEdge>> _graph =
            new Dictionary<int, List<NetworkEdge>>();
        private readonly Dictionary<long, TraySegment> _trays =
            new Dictionary<long, TraySegment>();
        private readonly Dictionary<long, int[]> _trayNodes =
            new Dictionary<long, int[]>();
        private readonly Dictionary<Cell, List<long>> _trayCells =
            new Dictionary<Cell, List<long>>();

        public int FittingsBridged { get; private set; }

        /// <summary>
        /// How many separate groups of joined-up trays the model has. More
        /// than one means the trays do not form a single network, which is
        /// the usual reason a route ends up crossing open space.
        /// </summary>
        public int ComponentCount { get; private set; }

        /// <summary>Hops added across gaps between those groups.</summary>
        public int BridgeEdgeCount { get; private set; }

        /// <summary>The real length of every bridge hop added, in feet.</summary>
        public double BridgeLengthAdded { get; private set; }

        public TrayNetwork(
            IEnumerable<TraySegment> trays,
            IEnumerable<FittingLink> fittings,
            double toleranceMm)
        {
            _tolerance = Units.MmToFeet(toleranceMm);
            _cellSize = Units.MmToFeet(5000.0);
            foreach (TraySegment tray in trays)
            {
                if (tray == null || tray.Length <= Geometry.Epsilon)
                {
                    continue;
                }
                int startNode = NodeAt(tray.Start);
                int endNode = NodeAt(tray.End);
                if (startNode == endNode)
                {
                    continue;
                }
                _trays[tray.Id] = tray;
                _trayNodes[tray.Id] = new int[] { startNode, endNode };
                AddEdge(startNode, endNode, tray.Length, EdgeKind.Tray, tray.Id,
                    new Pt[] { tray.Start, tray.End });
            }
            IndexTrays();
            if (fittings != null)
            {
                BridgeFittings(fittings);
            }
        }

        public int NodeCount
        {
            get { return _nodePoints.Count; }
        }

        public int TrayCount
        {
            get { return _trays.Count; }
        }

        public IEnumerable<TraySegment> Trays
        {
            get { return _trays.Values; }
        }

        public Pt NodePoint(int node)
        {
            return _nodePoints[node];
        }

        // ----- building ------------------------------------------------------
        private Cell CellOf(Pt point, double size)
        {
            return new Cell(
                (int)Math.Floor(point.X / size),
                (int)Math.Floor(point.Y / size),
                (int)Math.Floor(point.Z / size));
        }

        /// <summary>The node at this point, within the tolerance, or a new one.</summary>
        private int NodeAt(Pt point)
        {
            Cell cell = CellOf(point, _tolerance);
            int nearest = -1;
            double nearestDistance = _tolerance;
            for (int dx = -1; dx <= 1; dx++)
            {
                for (int dy = -1; dy <= 1; dy++)
                {
                    for (int dz = -1; dz <= 1; dz++)
                    {
                        List<int> bucket;
                        if (!_nodeCells.TryGetValue(
                                new Cell(cell.X + dx, cell.Y + dy, cell.Z + dz),
                                out bucket))
                        {
                            continue;
                        }
                        for (int index = 0; index < bucket.Count; index++)
                        {
                            double distance =
                                point.DistanceTo(_nodePoints[bucket[index]]);
                            if (distance <= nearestDistance)
                            {
                                nearest = bucket[index];
                                nearestDistance = distance;
                            }
                        }
                    }
                }
            }
            if (nearest >= 0)
            {
                return nearest;
            }
            int node = _nodePoints.Count;
            _nodePoints.Add(point);
            List<int> cellNodes;
            if (!_nodeCells.TryGetValue(cell, out cellNodes))
            {
                cellNodes = new List<int>();
                _nodeCells[cell] = cellNodes;
            }
            cellNodes.Add(node);
            return node;
        }

        /// <summary>The node at this point, or -1 when there is none.</summary>
        private int ExistingNodeAt(Pt point)
        {
            Cell cell = CellOf(point, _tolerance);
            int nearest = -1;
            double nearestDistance = _tolerance;
            for (int dx = -1; dx <= 1; dx++)
            {
                for (int dy = -1; dy <= 1; dy++)
                {
                    for (int dz = -1; dz <= 1; dz++)
                    {
                        List<int> bucket;
                        if (!_nodeCells.TryGetValue(
                                new Cell(cell.X + dx, cell.Y + dy, cell.Z + dz),
                                out bucket))
                        {
                            continue;
                        }
                        for (int index = 0; index < bucket.Count; index++)
                        {
                            double distance =
                                point.DistanceTo(_nodePoints[bucket[index]]);
                            if (distance <= nearestDistance)
                            {
                                nearest = bucket[index];
                                nearestDistance = distance;
                            }
                        }
                    }
                }
            }
            return nearest;
        }

        private void AddEdge(int nodeA, int nodeB, double weight,
            EdgeKind kind, long ownerId, Pt[] points)
        {
            AddEdge(nodeA, nodeB, weight, weight, kind, ownerId, points);
        }

        private void AddEdge(int nodeA, int nodeB, double weight,
            double realLength, EdgeKind kind, long ownerId, Pt[] points)
        {
            if (nodeA == nodeB)
            {
                return;
            }
            NetworkEdge forward = new NetworkEdge();
            forward.Kind = kind;
            forward.OwnerId = ownerId;
            forward.FromNode = nodeA;
            forward.ToNode = nodeB;
            forward.Weight = weight;
            forward.RealLength = realLength;
            forward.Points = points;
            NetworkEdge backward = new NetworkEdge();
            backward.Kind = kind;
            backward.OwnerId = ownerId;
            backward.FromNode = nodeB;
            backward.ToNode = nodeA;
            backward.Weight = weight;
            backward.RealLength = realLength;
            backward.Points = Reversed(points);
            Attach(nodeA, forward);
            Attach(nodeB, backward);
        }

        private void Attach(int node, NetworkEdge edge)
        {
            List<NetworkEdge> edges;
            if (!_graph.TryGetValue(node, out edges))
            {
                edges = new List<NetworkEdge>();
                _graph[node] = edges;
            }
            edges.Add(edge);
        }

        private static Pt[] Reversed(Pt[] points)
        {
            if (points == null)
            {
                return null;
            }
            Pt[] copy = new Pt[points.Length];
            for (int index = 0; index < points.Length; index++)
            {
                copy[index] = points[points.Length - 1 - index];
            }
            return copy;
        }

        /// <summary>A tee or bend joins the tray ends plugged into it.</summary>
        private void BridgeFittings(IEnumerable<FittingLink> fittings)
        {
            foreach (FittingLink fitting in fittings)
            {
                if (fitting == null || fitting.ConnectorPoints == null)
                {
                    continue;
                }
                List<int> nodes = new List<int>();
                List<Pt> points = new List<Pt>();
                foreach (Pt point in fitting.ConnectorPoints)
                {
                    int node = ExistingNodeAt(point);
                    if (node < 0 || nodes.Contains(node))
                    {
                        continue;
                    }
                    nodes.Add(node);
                    points.Add(point);
                }
                if (nodes.Count < 2)
                {
                    continue;
                }
                FittingsBridged++;
                for (int first = 0; first < nodes.Count; first++)
                {
                    for (int second = first + 1; second < nodes.Count; second++)
                    {
                        Pt[] edgePoints = new Pt[]
                        {
                            _nodePoints[nodes[first]],
                            points[first],
                            points[second],
                            _nodePoints[nodes[second]]
                        };
                        double weight = 0.0;
                        for (int step = 0; step < edgePoints.Length - 1; step++)
                        {
                            weight += edgePoints[step]
                                .DistanceTo(edgePoints[step + 1]);
                        }
                        AddEdge(nodes[first], nodes[second], weight,
                            EdgeKind.Fitting, fitting.Id, edgePoints);
                    }
                }
            }
        }

        private void IndexTrays()
        {
            foreach (KeyValuePair<long, TraySegment> entry in _trays)
            {
                TraySegment tray = entry.Value;
                Pt low = new Pt(
                    Math.Min(tray.Start.X, tray.End.X),
                    Math.Min(tray.Start.Y, tray.End.Y),
                    Math.Min(tray.Start.Z, tray.End.Z));
                Pt high = new Pt(
                    Math.Max(tray.Start.X, tray.End.X),
                    Math.Max(tray.Start.Y, tray.End.Y),
                    Math.Max(tray.Start.Z, tray.End.Z));
                foreach (Cell cell in CellsBetween(low, high))
                {
                    List<long> bucket;
                    if (!_trayCells.TryGetValue(cell, out bucket))
                    {
                        bucket = new List<long>();
                        _trayCells[cell] = bucket;
                    }
                    bucket.Add(entry.Key);
                }
            }
        }

        private List<Cell> CellsBetween(Pt low, Pt high)
        {
            List<Cell> cells = new List<Cell>();
            Cell first = CellOf(low, _cellSize);
            Cell last = CellOf(high, _cellSize);
            for (int x = first.X; x <= last.X; x++)
            {
                for (int y = first.Y; y <= last.Y; y++)
                {
                    for (int z = first.Z; z <= last.Z; z++)
                    {
                        cells.Add(new Cell(x, y, z));
                    }
                }
            }
            return cells;
        }

        /// <summary>
        /// The trays worth projecting onto: the ones near these points, with
        /// the search widened until something turns up, because a plant room
        /// panel can be a long way from the nearest tray.
        /// </summary>
        private List<long> SearchTrays(IList<Pt> points)
        {
            double[] radiiMm = new double[] { 5000.0, 15000.0, 40000.0, 100000.0 };
            for (int step = 0; step < radiiMm.Length; step++)
            {
                double radius = Units.MmToFeet(radiiMm[step]);
                HashSet<long> found = new HashSet<long>();
                for (int index = 0; index < points.Count; index++)
                {
                    Pt point = points[index];
                    Pt low = new Pt(point.X - radius, point.Y - radius,
                        point.Z - radius);
                    Pt high = new Pt(point.X + radius, point.Y + radius,
                        point.Z + radius);
                    foreach (Cell cell in CellsBetween(low, high))
                    {
                        List<long> bucket;
                        if (_trayCells.TryGetValue(cell, out bucket))
                        {
                            for (int item = 0; item < bucket.Count; item++)
                            {
                                found.Add(bucket[item]);
                            }
                        }
                    }
                }
                if (found.Count > 0)
                {
                    return new List<long>(found);
                }
            }
            return new List<long>(_trays.Keys);
        }

        // ----- joining up the gaps ---------------------------------------------
        /// <summary>Which group of joined-up trays each node belongs to.</summary>
        private int[] Components(out int count)
        {
            int[] component = new int[_nodePoints.Count];
            for (int index = 0; index < component.Length; index++)
            {
                component[index] = -1;
            }
            count = 0;
            Queue<int> pending = new Queue<int>();
            for (int start = 0; start < component.Length; start++)
            {
                if (component[start] >= 0)
                {
                    continue;
                }
                component[start] = count;
                pending.Enqueue(start);
                while (pending.Count > 0)
                {
                    int node = pending.Dequeue();
                    List<NetworkEdge> edges;
                    if (!_graph.TryGetValue(node, out edges))
                    {
                        continue;
                    }
                    for (int index = 0; index < edges.Count; index++)
                    {
                        int next = edges[index].ToNode;
                        if (component[next] < 0)
                        {
                            component[next] = count;
                            pending.Enqueue(next);
                        }
                    }
                }
                count++;
            }
            return component;
        }

        /// <summary>
        /// Join groups of trays that stop short of each other, so a route can
        /// run along the trays, hop a gap and carry on along the next run,
        /// instead of abandoning the trays and crossing open space.
        ///
        /// A hop costs 'preference' times its length, so the router takes a
        /// much longer way round on trays rather than a short hop through the
        /// air, and crosses a gap only where there is really no tray path.
        /// The hop is still reported at its true length.
        /// </summary>
        public void BridgeGaps(double maxGapMm, double preference)
        {
            _preference = preference > 1.0 ? preference : 1.0;
            int before;
            int[] component = Components(out before);
            ComponentCount = before;
            if (before <= 1 || maxGapMm <= 0.0 || _nodePoints.Count == 0)
            {
                return;
            }
            double maxGap = Units.MmToFeet(maxGapMm);
            double weighting = preference > 1.0 ? preference : 1.0;

            // A coarse index, so each node only looks at the nodes near it.
            double cell = Math.Max(maxGap, Units.MmToFeet(500.0));
            Dictionary<Cell, List<int>> buckets =
                new Dictionary<Cell, List<int>>();
            for (int node = 0; node < _nodePoints.Count; node++)
            {
                Cell key = CellOf(_nodePoints[node], cell);
                List<int> bucket;
                if (!buckets.TryGetValue(key, out bucket))
                {
                    bucket = new List<int>();
                    buckets[key] = bucket;
                }
                bucket.Add(node);
            }

            HashSet<string> added = new HashSet<string>();
            for (int node = 0; node < _nodePoints.Count; node++)
            {
                Pt point = _nodePoints[node];
                Cell home = CellOf(point, cell);
                // The two nearest ends in other groups: one is the usual
                // case, and two lets a tray run broken in the middle be
                // rejoined from both sides.
                int firstNode = -1;
                int secondNode = -1;
                double firstGap = double.MaxValue;
                double secondGap = double.MaxValue;
                for (int dx = -1; dx <= 1; dx++)
                {
                    for (int dy = -1; dy <= 1; dy++)
                    {
                        for (int dz = -1; dz <= 1; dz++)
                        {
                            List<int> bucket;
                            if (!buckets.TryGetValue(new Cell(
                                    home.X + dx, home.Y + dy, home.Z + dz),
                                    out bucket))
                            {
                                continue;
                            }
                            for (int item = 0; item < bucket.Count; item++)
                            {
                                int other = bucket[item];
                                if (other == node
                                        || component[other] == component[node])
                                {
                                    continue;
                                }
                                double gap = point.DistanceTo(
                                    _nodePoints[other]);
                                if (gap > maxGap)
                                {
                                    continue;
                                }
                                if (gap < firstGap)
                                {
                                    secondGap = firstGap;
                                    secondNode = firstNode;
                                    firstGap = gap;
                                    firstNode = other;
                                }
                                else if (gap < secondGap)
                                {
                                    secondGap = gap;
                                    secondNode = other;
                                }
                            }
                        }
                    }
                }
                int[] chosen = new int[] { firstNode, secondNode };
                for (int index = 0; index < chosen.Length; index++)
                {
                    int other = chosen[index];
                    if (other < 0)
                    {
                        continue;
                    }
                    string key = Math.Min(node, other).ToString() + ":"
                        + Math.Max(node, other).ToString();
                    if (!added.Add(key))
                    {
                        continue;
                    }
                    Pt from = _nodePoints[node];
                    Pt to = _nodePoints[other];
                    // The cable turns the corner rather than cutting across.
                    double length = Geometry.OrthogonalDistance(from, to);
                    Pt[] points = Geometry.OrthogonalPoints(from, to).ToArray();
                    AddEdge(node, other, length * weighting, length,
                        EdgeKind.Bridge, -1, points);
                    BridgeEdgeCount++;
                    BridgeLengthAdded += length;
                }
            }
            int after;
            Components(out after);
            ComponentCount = after;
        }

        // ----- attaching ------------------------------------------------------
        /// <summary>Where this equipment joins the network, cheapest first.</summary>
        public List<Attachment> Attach(IList<Pt> equipmentPoints)
        {
            List<Attachment> candidates = new List<Attachment>();
            if (equipmentPoints == null || equipmentPoints.Count == 0)
            {
                return candidates;
            }
            List<long> searchIds = SearchTrays(equipmentPoints);
            for (int trayIndex = 0; trayIndex < searchIds.Count; trayIndex++)
            {
                TraySegment tray = _trays[searchIds[trayIndex]];
                int[] nodes = _trayNodes[tray.Id];
                double trayLength = tray.Length;
                bool vertical = Geometry.IsVertical(
                    tray.Start, tray.End, _tolerance);
                for (int pointIndex = 0;
                     pointIndex < equipmentPoints.Count;
                     pointIndex++)
                {
                    Pt equipmentPoint = equipmentPoints[pointIndex];
                    Pt trayPoint;
                    double along;
                    Geometry.ProjectOntoSegment(
                        tray.Start, tray.End, equipmentPoint,
                        out trayPoint, out along);
                    double gap = Geometry.OrthogonalDistance(
                        equipmentPoint, trayPoint);
                    for (int end = 0; end < 2; end++)
                    {
                        double endAlong = end == 0 ? 0.0 : trayLength;
                        double partial = Math.Abs(along - endAlong);
                        Pt endPoint = end == 0 ? tray.Start : tray.End;
                        Pt[] partialPoints = partial <= Geometry.Epsilon
                            ? new Pt[] { trayPoint }
                            : new Pt[] { trayPoint, endPoint };
                        Attachment attachment = new Attachment();
                        attachment.Node = nodes[end];
                        attachment.Gap = gap;
                        attachment.PartialLength = partial;
                        attachment.EquipmentPoint = equipmentPoint;
                        attachment.TrayPoint = trayPoint;
                        attachment.TrayId = tray.Id;
                        attachment.Vertical = vertical;
                        attachment.PartialPoints = partialPoints;
                        attachment.GapWeight = _preference;
                        candidates.Add(attachment);
                    }
                }
            }
            candidates = PreferRisers(candidates, equipmentPoints);
            candidates = NearestDropOnly(candidates);
            candidates.Sort(delegate (Attachment a, Attachment b)
            {
                return a.Cost.CompareTo(b.Cost);
            });
            List<Attachment> chosen = new List<Attachment>();
            HashSet<int> seenNodes = new HashSet<int>();
            for (int index = 0; index < candidates.Count; index++)
            {
                if (!seenNodes.Add(candidates[index].Node))
                {
                    continue;
                }
                chosen.Add(candidates[index]);
                if (chosen.Count >= MaxAttachments)
                {
                    break;
                }
            }
            return chosen;
        }

        /// <summary>
        /// Equipment on another level should drop onto a riser, not fly to a
        /// tray overhead on a different floor.
        /// </summary>
        private List<Attachment> PreferRisers(
            List<Attachment> candidates, IList<Pt> equipmentPoints)
        {
            if (candidates.Count == 0)
            {
                return candidates;
            }
            List<Attachment> kept = new List<Attachment>();
            for (int index = 0; index < equipmentPoints.Count; index++)
            {
                Pt equipmentPoint = equipmentPoints[index];
                List<Attachment> mine = new List<Attachment>();
                List<Attachment> risers = new List<Attachment>();
                List<Attachment> sameLevel = new List<Attachment>();
                List<Attachment> offLevel = new List<Attachment>();
                for (int item = 0; item < candidates.Count; item++)
                {
                    Attachment candidate = candidates[item];
                    if (candidate.EquipmentPoint.DistanceTo(equipmentPoint)
                            > Geometry.Epsilon)
                    {
                        continue;
                    }
                    mine.Add(candidate);
                    double difference = Math.Abs(
                        equipmentPoint.Z - candidate.TrayPoint.Z);
                    if (candidate.Vertical && difference <= _tolerance)
                    {
                        risers.Add(candidate);
                    }
                    else if (difference <= _tolerance)
                    {
                        sameLevel.Add(candidate);
                    }
                    else
                    {
                        offLevel.Add(candidate);
                    }
                }
                bool riserWins = risers.Count > 0 && offLevel.Count > 0
                    && sameLevel.Count == 0
                    && SmallestGap(risers) < SmallestGap(offLevel);
                kept.AddRange(riserWins ? risers : mine);
            }
            return kept;
        }

        private static double SmallestGap(List<Attachment> candidates)
        {
            double smallest = double.MaxValue;
            for (int index = 0; index < candidates.Count; index++)
            {
                if (candidates[index].Gap < smallest)
                {
                    smallest = candidates[index].Gap;
                }
            }
            return smallest;
        }

        /// <summary>Keep the attachments at the nearest tray, both ends of it.</summary>
        private List<Attachment> NearestDropOnly(List<Attachment> candidates)
        {
            if (candidates.Count == 0)
            {
                return candidates;
            }
            double reach = SmallestGap(candidates)
                + Units.MmToFeet(AttachGapSlackMm);
            List<Attachment> kept = new List<Attachment>();
            for (int index = 0; index < candidates.Count; index++)
            {
                if (candidates[index].Gap <= reach)
                {
                    kept.Add(candidates[index]);
                }
            }
            return kept;
        }

        // ----- distances ------------------------------------------------------
        /// <summary>
        /// One Dijkstra pass for all of an item's attachments. Each one seeds
        /// the queue with its own cost, so the result is the cheapest way out
        /// of that panel to every node - and every other panel is then a
        /// dictionary lookup rather than another pass.
        /// </summary>
        public Reach DistancesFrom(IList<Attachment> attachments)
        {
            Reach reach = new Reach();
            MinHeap queue = new MinHeap();
            for (int index = 0; index < attachments.Count; index++)
            {
                Attachment attachment = attachments[index];
                double cost = attachment.WeightedCost;
                double existing;
                if (reach.Distances.TryGetValue(attachment.Node, out existing)
                        && existing <= cost)
                {
                    continue;
                }
                reach.Distances[attachment.Node] = cost;
                reach.Seeds[attachment.Node] = attachment.Node;
                queue.Push(cost, attachment.Node);
            }
            while (queue.Count > 0)
            {
                double current;
                int node;
                queue.Pop(out current, out node);
                double best;
                if (reach.Distances.TryGetValue(node, out best)
                        && current > best + Geometry.Epsilon)
                {
                    continue;       // a stale queue entry
                }
                List<NetworkEdge> edges;
                if (!_graph.TryGetValue(node, out edges))
                {
                    continue;
                }
                for (int index = 0; index < edges.Count; index++)
                {
                    NetworkEdge edge = edges[index];
                    double candidate = current + edge.Weight;
                    double known;
                    if (reach.Distances.TryGetValue(edge.ToNode, out known)
                            && known <= candidate)
                    {
                        continue;
                    }
                    reach.Distances[edge.ToNode] = candidate;
                    reach.Previous[edge.ToNode] = edge;
                    reach.Seeds[edge.ToNode] = reach.Seeds[node];
                    queue.Push(candidate, edge.ToNode);
                }
            }
            return reach;
        }

        /// <summary>The edges from the seed attachment node to this node.</summary>
        public List<NetworkEdge> PathTo(Reach reach, int node)
        {
            List<NetworkEdge> edges = new List<NetworkEdge>();
            int cursor = node;
            int guard = 0;
            NetworkEdge edge;
            while (reach.Previous.TryGetValue(cursor, out edge))
            {
                edges.Add(edge);
                cursor = edge.FromNode;
                guard++;
                if (guard > 1000000)
                {
                    break;          // a cycle should be impossible, but never hang
                }
            }
            edges.Reverse();
            return edges;
        }

        /// <summary>The tray centre lines a path follows, in order.</summary>
        public List<Pt> EdgePoints(IList<NetworkEdge> edges)
        {
            List<Pt> points = new List<Pt>();
            for (int index = 0; index < edges.Count; index++)
            {
                Pt[] edgePoints = edges[index].Points;
                if (edgePoints == null)
                {
                    continue;
                }
                for (int item = 0; item < edgePoints.Length; item++)
                {
                    points.Add(edgePoints[item]);
                }
            }
            return points;
        }
    }

    /// <summary>
    /// A binary heap. The framework has no priority queue before .NET 6, and
    /// this add-in builds for .NET Framework 4.8 as well.
    /// </summary>
    internal class MinHeap
    {
        private struct Entry
        {
            public double Key;
            public int Value;
        }

        private readonly List<Entry> _items = new List<Entry>();

        public int Count
        {
            get { return _items.Count; }
        }

        public void Push(double key, int value)
        {
            Entry entry = new Entry();
            entry.Key = key;
            entry.Value = value;
            _items.Add(entry);
            int child = _items.Count - 1;
            while (child > 0)
            {
                int parent = (child - 1) / 2;
                if (_items[parent].Key <= _items[child].Key)
                {
                    break;
                }
                Entry swap = _items[parent];
                _items[parent] = _items[child];
                _items[child] = swap;
                child = parent;
            }
        }

        public void Pop(out double key, out int value)
        {
            Entry top = _items[0];
            key = top.Key;
            value = top.Value;
            int last = _items.Count - 1;
            _items[0] = _items[last];
            _items.RemoveAt(last);
            int parent = 0;
            while (true)
            {
                int left = parent * 2 + 1;
                int right = left + 1;
                int smallest = parent;
                if (left < _items.Count
                        && _items[left].Key < _items[smallest].Key)
                {
                    smallest = left;
                }
                if (right < _items.Count
                        && _items[right].Key < _items[smallest].Key)
                {
                    smallest = right;
                }
                if (smallest == parent)
                {
                    break;
                }
                Entry swap = _items[parent];
                _items[parent] = _items[smallest];
                _items[smallest] = swap;
                parent = smallest;
            }
        }
    }
}
