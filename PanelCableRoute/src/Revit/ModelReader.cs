// -----------------------------------------------------------------------------
// Reading the model into the plain data the core works on.
//
// This is the only place that talks to the Revit API for geometry and
// parameters, so everything below Core stays testable.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using PanelCableRoute.Core;

namespace PanelCableRoute.Revit
{
    public static class ModelReader
    {
        /// <summary>Parameters a panel or a piece of equipment is named by.</summary>
        private static readonly string[] NameParameters = new string[]
        {
            "Panel Name", "Panel", "Mark", "Equipment Name", "Type Mark"
        };

        // ----- ids and units -------------------------------------------------
        /// <summary>
        /// The number behind an ElementId. IntegerValue was removed in Revit
        /// 2026 and Value arrived in 2024, so the choice is made at build time
        /// from the RevitVersion property.
        /// </summary>
        public static long IdOf(ElementId id)
        {
            if (id == null)
            {
                return -1;
            }
#if REVIT_ELEMENTID_VALUE
            return id.Value;
#else
            return id.IntegerValue;
#endif
        }

        public static long IdOf(Element element)
        {
            return element == null ? -1 : IdOf(element.Id);
        }

        public static ElementId ToElementId(long value)
        {
#if REVIT_ELEMENTID_VALUE
            return new ElementId(value);
#else
            return new ElementId((int)value);
#endif
        }

        public static Pt ToPt(XYZ point)
        {
            return new Pt(point.X, point.Y, point.Z);
        }

        public static XYZ ToXyz(Pt point)
        {
            return new XYZ(point.X, point.Y, point.Z);
        }

        /// <summary>Internal units to amperes, where this Revit knows the unit.</summary>
        public static double Amperes(double internalValue)
        {
            try
            {
                return UnitUtils.ConvertFromInternalUnits(
                    internalValue, UnitTypeId.Amperes);
            }
            catch (Exception)
            {
                return internalValue;
            }
        }

        public static double Volts(double internalValue)
        {
            try
            {
                return UnitUtils.ConvertFromInternalUnits(
                    internalValue, UnitTypeId.Volts);
            }
            catch (Exception)
            {
                return internalValue;
            }
        }

        // ----- parameters ----------------------------------------------------
        private static string TextOf(Parameter parameter)
        {
            if (parameter == null)
            {
                return "";
            }
            try
            {
                string value = parameter.AsString();
                if (!string.IsNullOrEmpty(value))
                {
                    return value;
                }
                value = parameter.AsValueString();
                return string.IsNullOrEmpty(value) ? "" : value;
            }
            catch (Exception)
            {
                return "";
            }
        }

        private static string Lookup(Element element, string name)
        {
            try
            {
                return TextOf(element.LookupParameter(name));
            }
            catch (Exception)
            {
                return "";
            }
        }

        private static string Builtin(Element element, BuiltInParameter which)
        {
            try
            {
                return TextOf(element.get_Parameter(which));
            }
            catch (Exception)
            {
                return "";
            }
        }

        public static string NameOf(Element element)
        {
            for (int index = 0; index < NameParameters.Length; index++)
            {
                string value = Lookup(element, NameParameters[index]);
                if (!string.IsNullOrEmpty(value))
                {
                    return value;
                }
            }
            try
            {
                if (!string.IsNullOrEmpty(element.Name))
                {
                    return element.Name;
                }
            }
            catch (Exception)
            {
                // Name throws for a few element kinds.
            }
            return "Element " + IdOf(element).ToString();
        }

        public static string CommentsOf(Element element)
        {
            string value = Builtin(
                element, BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
            if (!string.IsNullOrEmpty(value))
            {
                return value;
            }
            return Lookup(element, "Comments");
        }

        public static string TypeCommentsOf(Document doc, Element element)
        {
            try
            {
                Element symbol = doc.GetElement(element.GetTypeId());
                if (symbol == null)
                {
                    return "";
                }
                string value = Builtin(
                    symbol, BuiltInParameter.ALL_MODEL_TYPE_COMMENTS);
                if (!string.IsNullOrEmpty(value))
                {
                    return value;
                }
                return Lookup(symbol, "Comments");
            }
            catch (Exception)
            {
                return "";
            }
        }

        private static string RatingOf(Document doc, Element element)
        {
            string value = Lookup(element, "JAC-EL-Rating");
            if (!string.IsNullOrEmpty(value))
            {
                return value;
            }
            try
            {
                Element symbol = doc.GetElement(element.GetTypeId());
                return symbol == null ? "" : Lookup(symbol, "JAC-EL-Rating");
            }
            catch (Exception)
            {
                return "";
            }
        }

        private static string TypeNameText(Document doc, Element element)
        {
            List<string> names = new List<string>();
            try
            {
                if (!string.IsNullOrEmpty(element.Name))
                {
                    names.Add(element.Name);
                }
            }
            catch (Exception)
            {
            }
            try
            {
                FamilySymbol symbol =
                    doc.GetElement(element.GetTypeId()) as FamilySymbol;
                if (symbol != null)
                {
                    names.Add(symbol.Name);
                    if (symbol.Family != null)
                    {
                        names.Add(symbol.Family.Name);
                    }
                }
            }
            catch (Exception)
            {
            }
            return string.Join(" ", names.ToArray()).ToLowerInvariant();
        }

        /// <summary>
        /// An isolator is tagged in Comments or JAC-EL-Rating, or is an
        /// electrical fixture whose family or type is named one.
        /// </summary>
        public static bool IsIsolator(Document doc, Element element)
        {
            string text = (CommentsOf(element) + " "
                + TypeCommentsOf(doc, element)).ToLowerInvariant();
            if (text.Contains("isolator"))
            {
                return true;
            }
            if (RatingOf(doc, element).ToLowerInvariant().Contains("isolator"))
            {
                return true;
            }
            if (!IsCategory(element, BuiltInCategory.OST_ElectricalFixtures))
            {
                return false;
            }
            return TypeNameText(doc, element).Contains("isolator");
        }

        public static bool IsCategory(Element element, BuiltInCategory which)
        {
            try
            {
                if (element.Category == null)
                {
                    return false;
                }
                return IdOf(element.Category.Id) == (long)(int)which;
            }
            catch (Exception)
            {
                return false;
            }
        }

        public static string LevelNameOf(Document doc, Element element)
        {
            try
            {
                ElementId levelId = element.LevelId;
                if (levelId != null && levelId != ElementId.InvalidElementId)
                {
                    Element level = doc.GetElement(levelId);
                    if (level != null)
                    {
                        return level.Name;
                    }
                }
            }
            catch (Exception)
            {
            }
            try
            {
                Parameter parameter = element.get_Parameter(
                    BuiltInParameter.FAMILY_LEVEL_PARAM);
                if (parameter != null)
                {
                    Element level = doc.GetElement(parameter.AsElementId());
                    if (level != null)
                    {
                        return level.Name;
                    }
                }
            }
            catch (Exception)
            {
            }
            return "";
        }

        public static ElementId LevelIdOf(Element element)
        {
            try
            {
                ElementId levelId = element.LevelId;
                if (levelId != null && levelId != ElementId.InvalidElementId)
                {
                    return levelId;
                }
            }
            catch (Exception)
            {
            }
            try
            {
                Parameter parameter = element.get_Parameter(
                    BuiltInParameter.FAMILY_LEVEL_PARAM);
                if (parameter != null)
                {
                    ElementId levelId = parameter.AsElementId();
                    if (levelId != null
                            && levelId != ElementId.InvalidElementId)
                    {
                        return levelId;
                    }
                }
            }
            catch (Exception)
            {
            }
            return null;
        }

        // ----- connectors and points -----------------------------------------
        public static List<Connector> ConnectorsOf(Element element)
        {
            List<Connector> found = new List<Connector>();
            try
            {
                FamilyInstance instance = element as FamilyInstance;
                if (instance != null && instance.MEPModel != null
                        && instance.MEPModel.ConnectorManager != null)
                {
                    foreach (Connector connector in
                        instance.MEPModel.ConnectorManager.Connectors)
                    {
                        found.Add(connector);
                    }
                    return found;
                }
            }
            catch (Exception)
            {
            }
            try
            {
                MEPCurve curve = element as MEPCurve;
                if (curve != null && curve.ConnectorManager != null)
                {
                    foreach (Connector connector in
                        curve.ConnectorManager.Connectors)
                    {
                        found.Add(connector);
                    }
                }
            }
            catch (Exception)
            {
            }
            return found;
        }

        /// <summary>
        /// Where a cable can leave an element: its connectors, else its
        /// insertion point, else the ends of its curve, else the centre of its
        /// bounding box.
        /// </summary>
        public static List<Pt> PointsOf(Element element)
        {
            List<Pt> points = new List<Pt>();
            List<Connector> connectors = ConnectorsOf(element);
            for (int index = 0; index < connectors.Count; index++)
            {
                try
                {
                    points.Add(ToPt(connectors[index].Origin));
                }
                catch (Exception)
                {
                }
            }
            if (points.Count > 0)
            {
                return points;
            }
            try
            {
                LocationPoint locationPoint = element.Location as LocationPoint;
                if (locationPoint != null)
                {
                    points.Add(ToPt(locationPoint.Point));
                    return points;
                }
            }
            catch (Exception)
            {
            }
            try
            {
                LocationCurve locationCurve = element.Location as LocationCurve;
                if (locationCurve != null && locationCurve.Curve != null)
                {
                    points.Add(ToPt(locationCurve.Curve.GetEndPoint(0)));
                    points.Add(ToPt(locationCurve.Curve.GetEndPoint(1)));
                    return points;
                }
            }
            catch (Exception)
            {
            }
            try
            {
                BoundingBoxXYZ box = element.get_BoundingBox(null);
                if (box != null)
                {
                    points.Add(new Pt(
                        (box.Min.X + box.Max.X) / 2.0,
                        (box.Min.Y + box.Max.Y) / 2.0,
                        (box.Min.Z + box.Max.Z) / 2.0));
                }
            }
            catch (Exception)
            {
            }
            return points;
        }

        // ----- the model ------------------------------------------------------
        /// <summary>Every cable tray, and the fittings that join their ends.</summary>
        public static void ReadTrays(
            Document doc, out List<TraySegment> trays,
            out List<FittingLink> fittings)
        {
            trays = new List<TraySegment>();
            Dictionary<long, FittingLink> byFitting =
                new Dictionary<long, FittingLink>();
            FilteredElementCollector collector =
                new FilteredElementCollector(doc)
                    .OfCategory(BuiltInCategory.OST_CableTray)
                    .WhereElementIsNotElementType();
            foreach (Element element in collector)
            {
                LocationCurve location = element.Location as LocationCurve;
                if (location == null || location.Curve == null)
                {
                    continue;
                }
                TraySegment tray = new TraySegment();
                tray.Id = IdOf(element);
                tray.Start = ToPt(location.Curve.GetEndPoint(0));
                tray.End = ToPt(location.Curve.GetEndPoint(1));
                tray.WidthMm = SizeMm(
                    element, BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM, 300.0);
                tray.HeightMm = SizeMm(
                    element, BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM, 100.0);
                trays.Add(tray);

                // A tray end plugged into a fitting: remember the point, so
                // the network can join the ends that fitting holds together.
                List<Connector> connectors = ConnectorsOf(element);
                for (int index = 0; index < connectors.Count; index++)
                {
                    Connector connector = connectors[index];
                    ConnectorSet references = null;
                    try
                    {
                        references = connector.AllRefs;
                    }
                    catch (Exception)
                    {
                        continue;
                    }
                    foreach (Connector reference in references)
                    {
                        Element owner;
                        try
                        {
                            owner = reference.Owner;
                        }
                        catch (Exception)
                        {
                            continue;
                        }
                        if (owner == null || IdOf(owner) == tray.Id)
                        {
                            continue;
                        }
                        if (!IsCategory(owner,
                                BuiltInCategory.OST_CableTrayFitting))
                        {
                            continue;
                        }
                        long fittingId = IdOf(owner);
                        FittingLink link;
                        if (!byFitting.TryGetValue(fittingId, out link))
                        {
                            link = new FittingLink();
                            link.Id = fittingId;
                            byFitting[fittingId] = link;
                        }
                        try
                        {
                            link.ConnectorPoints.Add(ToPt(connector.Origin));
                        }
                        catch (Exception)
                        {
                        }
                    }
                }
            }
            fittings = new List<FittingLink>(byFitting.Values);
        }

        private static double SizeMm(
            Element element, BuiltInParameter which, double fallbackMm)
        {
            try
            {
                Parameter parameter = element.get_Parameter(which);
                if (parameter != null)
                {
                    double value = Units.FeetToMm(parameter.AsDouble());
                    if (value > 1.0)
                    {
                        return value;
                    }
                }
            }
            catch (Exception)
            {
            }
            return fallbackMm;
        }

        private static EquipmentItem ItemFrom(Document doc, Element element)
        {
            EquipmentItem item = new EquipmentItem();
            item.Id = IdOf(element);
            item.Name = NameOf(element);
            item.LevelName = LevelNameOf(doc, element);
            try
            {
                item.CategoryName = element.Category != null
                    ? element.Category.Name : "";
            }
            catch (Exception)
            {
                item.CategoryName = "";
            }
            item.Points = PointsOf(element);
            try
            {
                BoundingBoxXYZ box = element.get_BoundingBox(null);
                if (box != null)
                {
                    item.HasBounds = true;
                    item.BoundsLow = ToPt(box.Min);
                    item.BoundsHigh = ToPt(box.Max);
                }
            }
            catch (Exception)
            {
            }
            return item;
        }

        /// <summary>Every electrical equipment item: panels, boards, transformers.</summary>
        public static List<EquipmentItem> ReadPanels(Document doc)
        {
            List<EquipmentItem> panels = new List<EquipmentItem>();
            FilteredElementCollector collector =
                new FilteredElementCollector(doc)
                    .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                    .WhereElementIsNotElementType();
            foreach (Element element in collector)
            {
                EquipmentItem item = ItemFrom(doc, element);
                item.IsIsolator = IsIsolator(doc, element);
                panels.Add(item);
            }
            panels.Sort(delegate (EquipmentItem a, EquipmentItem b)
            {
                return string.Compare(a.Name, b.Name,
                    StringComparison.OrdinalIgnoreCase);
            });
            return panels;
        }

        /// <summary>Isolators, wherever they are modelled.</summary>
        public static Dictionary<long, EquipmentItem> ReadIsolators(
            Document doc)
        {
            Dictionary<long, EquipmentItem> found =
                new Dictionary<long, EquipmentItem>();
            BuiltInCategory[] categories = new BuiltInCategory[]
            {
                BuiltInCategory.OST_ElectricalFixtures,
                BuiltInCategory.OST_ElectricalEquipment,
                BuiltInCategory.OST_GenericModel,
                BuiltInCategory.OST_SpecialityEquipment,
                BuiltInCategory.OST_MechanicalEquipment
            };
            for (int index = 0; index < categories.Length; index++)
            {
                FilteredElementCollector collector;
                try
                {
                    collector = new FilteredElementCollector(doc)
                        .OfCategory(categories[index])
                        .WhereElementIsNotElementType();
                }
                catch (Exception)
                {
                    continue;
                }
                foreach (Element element in collector)
                {
                    long id = IdOf(element);
                    if (found.ContainsKey(id) || !IsIsolator(doc, element))
                    {
                        continue;
                    }
                    EquipmentItem item = ItemFrom(doc, element);
                    item.IsIsolator = true;
                    found[id] = item;
                }
            }
            return found;
        }

        /// <summary>
        /// Every circuit as the panel that feeds it and one element it serves.
        /// This is the electrical topology already in the model.
        /// </summary>
        public static List<CircuitLink> ReadCircuits(Document doc)
        {
            List<CircuitLink> links = new List<CircuitLink>();
            FilteredElementCollector collector =
                new FilteredElementCollector(doc)
                    .OfClass(typeof(ElectricalSystem))
                    .WhereElementIsNotElementType();
            foreach (Element element in collector)
            {
                ElectricalSystem system = element as ElectricalSystem;
                if (system == null)
                {
                    continue;
                }
                Element baseEquipment;
                try
                {
                    baseEquipment = system.BaseEquipment;
                }
                catch (Exception)
                {
                    continue;
                }
                if (baseEquipment == null)
                {
                    continue;
                }
                long baseId = IdOf(baseEquipment);
                string number = "";
                string load = "";
                double current = 0.0;
                double voltage = 0.0;
                try
                {
                    number = system.CircuitNumber;
                }
                catch (Exception)
                {
                }
                try
                {
                    load = system.LoadName;
                }
                catch (Exception)
                {
                }
                try
                {
                    current = Amperes(system.Current);
                }
                catch (Exception)
                {
                }
                try
                {
                    voltage = Volts(system.Voltage);
                }
                catch (Exception)
                {
                }
                ElementSet members;
                try
                {
                    members = system.Elements;
                }
                catch (Exception)
                {
                    continue;
                }
                foreach (Element member in members)
                {
                    long memberId = IdOf(member);
                    if (memberId == baseId)
                    {
                        continue;
                    }
                    CircuitLink link = new CircuitLink();
                    link.BaseEquipmentId = baseId;
                    link.MemberId = memberId;
                    link.CircuitNumber = number == null ? "" : number;
                    link.LoadName = load == null ? "" : load;
                    link.Current = current;
                    link.Voltage = voltage;
                    links.Add(link);
                }
            }
            return links;
        }

        /// <summary>
        /// Which elements each of these elements is wired to, so the core can
        /// follow the connectors on to an isolator behind the plant.
        /// </summary>
        public static Dictionary<long, List<long>> ReadWiring(
            Document doc, IEnumerable<long> ids)
        {
            Dictionary<long, List<long>> wiring =
                new Dictionary<long, List<long>>();
            foreach (long id in ids)
            {
                if (wiring.ContainsKey(id))
                {
                    continue;
                }
                List<long> neighbours = new List<long>();
                wiring[id] = neighbours;
                Element element;
                try
                {
                    element = doc.GetElement(ToElementId(id));
                }
                catch (Exception)
                {
                    continue;
                }
                if (element == null)
                {
                    continue;
                }
                List<Connector> connectors = ConnectorsOf(element);
                for (int index = 0; index < connectors.Count; index++)
                {
                    ConnectorSet references;
                    try
                    {
                        references = connectors[index].AllRefs;
                    }
                    catch (Exception)
                    {
                        continue;
                    }
                    foreach (Connector reference in references)
                    {
                        try
                        {
                            Element owner = reference.Owner;
                            if (owner == null)
                            {
                                continue;
                            }
                            long ownerId = IdOf(owner);
                            if (ownerId != id && !neighbours.Contains(ownerId))
                            {
                                neighbours.Add(ownerId);
                            }
                        }
                        catch (Exception)
                        {
                        }
                    }
                }
            }
            return wiring;
        }

        /// <summary>Equipment and isolators by id, for resolving circuit members.</summary>
        public static Dictionary<long, EquipmentItem> ReadCircuitMembers(
            Document doc, IEnumerable<long> ids,
            Dictionary<long, EquipmentItem> known)
        {
            Dictionary<long, EquipmentItem> items =
                new Dictionary<long, EquipmentItem>();
            foreach (KeyValuePair<long, EquipmentItem> entry in known)
            {
                items[entry.Key] = entry.Value;
            }
            foreach (long id in ids)
            {
                if (items.ContainsKey(id))
                {
                    continue;
                }
                Element element;
                try
                {
                    element = doc.GetElement(ToElementId(id));
                }
                catch (Exception)
                {
                    continue;
                }
                if (element == null)
                {
                    continue;
                }
                EquipmentItem item = ItemFrom(doc, element);
                item.IsIsolator = IsIsolator(doc, element);
                items[id] = item;
            }
            return items;
        }
    }
}
