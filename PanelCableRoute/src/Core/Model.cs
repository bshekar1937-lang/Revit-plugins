// -----------------------------------------------------------------------------
// The plain data the core works on. The Revit layer fills these in from the
// model; nothing here knows about Revit.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;

namespace PanelCableRoute.Core
{
    /// <summary>One modelled cable tray, as a straight centre line.</summary>
    public class TraySegment
    {
        public long Id;
        public Pt Start;
        public Pt End;
        public double WidthMm;
        public double HeightMm;

        public double Length
        {
            get { return Start.DistanceTo(End); }
        }
    }

    /// <summary>
    /// A tray fitting and the connector points of the trays plugged into it.
    /// The network joins those tray ends through the fitting.
    /// </summary>
    public class FittingLink
    {
        public long Id;
        public List<Pt> ConnectorPoints = new List<Pt>();
    }

    /// <summary>A panel, a piece of equipment or an isolator.</summary>
    public class EquipmentItem
    {
        public long Id;
        public string Name = "";
        public string LevelName = "";
        public string CategoryName = "";
        public bool IsIsolator;
        /// <summary>Where a cable can leave: connectors, else the insertion point.</summary>
        public List<Pt> Points = new List<Pt>();
        public bool HasBounds;
        public Pt BoundsLow;
        public Pt BoundsHigh;

        public override string ToString()
        {
            return Name + " (" + Id.ToString() + ")";
        }
    }

    /// <summary>One circuit: the panel that feeds it and one element it serves.</summary>
    public class CircuitLink
    {
        public long BaseEquipmentId;
        public long MemberId;
        public string CircuitNumber = "";
        public string LoadName = "";
        /// <summary>Amperes, or zero when the circuit does not give one.</summary>
        public double Current;
        /// <summary>Volts, or zero when the circuit does not give one.</summary>
        public double Voltage;
    }

    public enum RouteKind
    {
        Circuit,
        Isolator,
        PanelPair
    }

    public static class RouteKindText
    {
        public static string Of(RouteKind kind)
        {
            if (kind == RouteKind.Circuit)
            {
                return "circuit";
            }
            if (kind == RouteKind.Isolator)
            {
                return "isolator";
            }
            return "panel pair";
        }
    }

    /// <summary>Everything the dialog collects.</summary>
    public class RouteOptions
    {
        public bool MeasurePanelPairs = true;
        public bool MeasureCircuits = true;
        public bool IncludeIsolators = true;
        public bool DrawDetailLines = true;

        /// <summary>Cable allowance, per cent of the measured route.</summary>
        public double AllowancePercent = 5.0;
        /// <summary>Tray ends this far apart count as connected, in mm.</summary>
        public double ToleranceMm = 100.0;
        /// <summary>Nominal voltage when a circuit does not give one.</summary>
        public double Voltage = 400.0;
        /// <summary>Load current when a circuit does not give one.</summary>
        public double Current = 32.0;
        /// <summary>Cable mV/A/m from the manufacturer's table.</summary>
        public double MilliVoltsPerAmpMetre = 1.1;
        /// <summary>Routes over this percentage are flagged.</summary>
        public double LimitPercent = 5.0;
        /// <summary>Panel pairs longer than this are dropped. 0 keeps every pair.</summary>
        public double MaxPairMetres = 0.0;
        /// <summary>
        /// Groups of trays whose ends come this close are joined, so a route
        /// can hop the gap and carry on along the next run. 0 turns it off.
        /// </summary>
        public double MaxBridgeMm = 3000.0;
        /// <summary>
        /// How much a route prefers trays. A hop through the air costs this
        /// many times its length, so at 8 the router will follow up to eight
        /// metres of tray rather than cross one metre of open space.
        /// </summary>
        public double TrayPreference = 8.0;

        /// <summary>Ids of the panels to measure. Empty means every panel.</summary>
        public HashSet<long> PanelIds = new HashSet<long>();

        public double ToleranceFeet
        {
            get { return Units.MmToFeet(ToleranceMm); }
        }

        public RouteOptions Copy()
        {
            RouteOptions copy = new RouteOptions();
            copy.MeasurePanelPairs = MeasurePanelPairs;
            copy.MeasureCircuits = MeasureCircuits;
            copy.IncludeIsolators = IncludeIsolators;
            copy.DrawDetailLines = DrawDetailLines;
            copy.AllowancePercent = AllowancePercent;
            copy.ToleranceMm = ToleranceMm;
            copy.Voltage = Voltage;
            copy.Current = Current;
            copy.MilliVoltsPerAmpMetre = MilliVoltsPerAmpMetre;
            copy.LimitPercent = LimitPercent;
            copy.MaxPairMetres = MaxPairMetres;
            copy.MaxBridgeMm = MaxBridgeMm;
            copy.TrayPreference = TrayPreference;
            copy.PanelIds = new HashSet<long>(PanelIds);
            return copy;
        }
    }

    /// <summary>Something that could not be measured, and why.</summary>
    public class RouteProblem
    {
        public EquipmentItem Item;
        public string Reason = "";
    }
}
