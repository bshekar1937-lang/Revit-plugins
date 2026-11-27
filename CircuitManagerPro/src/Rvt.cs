using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;

namespace CircuitManagerPro
{
    /// <summary>Small Revit helpers shared by the whole add-in.</summary>
    internal static class Rvt
    {
        // ElementId.IntegerValue was removed in Revit 2026; ElementId.Value exists from 2024.
        public static long Val(ElementId id)
        {
#if REVIT2024_OR_GREATER
            return id.Value;
#else
            return id.IntegerValue;
#endif
        }

        public static ElementId Id(long v)
        {
#if REVIT2024_OR_GREATER
            return new ElementId(v);
#else
            return new ElementId((int)v);
#endif
        }

        public static Element Get(Document doc, object tag)
            => tag is long v ? doc.GetElement(Id(v)) : null;

        // Categories that can be put on a power circuit
        public static readonly BuiltInCategory[] CircuitableBics =
        {
            BuiltInCategory.OST_LightingFixtures, BuiltInCategory.OST_LightingDevices,
            BuiltInCategory.OST_ElectricalFixtures, BuiltInCategory.OST_ElectricalEquipment,
            BuiltInCategory.OST_MechanicalEquipment, BuiltInCategory.OST_SpecialityEquipment,
            BuiltInCategory.OST_DataDevices, BuiltInCategory.OST_CommunicationDevices,
            BuiltInCategory.OST_FireAlarmDevices, BuiltInCategory.OST_SecurityDevices,
            BuiltInCategory.OST_NurseCallDevices, BuiltInCategory.OST_TelephoneDevices,
        };

        private static readonly HashSet<long> CircuitableIds =
            new HashSet<long>(CircuitableBics.Select(b => Val(new ElementId(b))));

        private static readonly long EquipmentCatId = Val(new ElementId(BuiltInCategory.OST_ElectricalEquipment));

        public static bool HasElectricalConnector(Element el)
        {
            ConnectorManager cm = (el as FamilyInstance)?.MEPModel?.ConnectorManager;
            if (cm == null) return false;
            foreach (Connector c in cm.Connectors)
                if (c.Domain == Domain.DomainElectrical) return true;
            return false;
        }

        public static bool IsCircuitable(Element el)
            => el is FamilyInstance && el.Category != null
               && CircuitableIds.Contains(Val(el.Category.Id)) && HasElectricalConnector(el);

        public static bool IsElectricalEquipment(Element el)
            => el?.Category != null && Val(el.Category.Id) == EquipmentCatId;

        public static List<ElectricalSystem> ElementSystems(Element el)
        {
            var list = new List<ElectricalSystem>();
            try
            {
                ISet<ElectricalSystem> s = (el as FamilyInstance)?.MEPModel?.GetElectricalSystems();
                if (s != null) list.AddRange(s);
            }
            catch (Exception) { }
            return list;
        }

        public static List<ElectricalSystem> PowerSystems(Element el)
            => ElementSystems(el).Where(s => s.SystemType == ElectricalSystemType.PowerCircuit).ToList();

        public static string ElementLabel(Element el)
        {
            try
            {
                string cat = el.Category?.Name ?? "?";
                string fam = (el as FamilyInstance)?.Symbol?.FamilyName ?? "";
                string mark = el.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)?.AsString();
                string txt = $"{cat}  :  {fam} - {el.Name}";
                if (!string.IsNullOrEmpty(mark)) txt += $"   [Mark {mark}]";
                return txt + $"   (id {Val(el.Id)})";
            }
            catch (Exception)
            {
                return $"Element {Val(el.Id)}";
            }
        }

        public static string PanelName(Element panel)
        {
            string s = panel.get_Parameter(BuiltInParameter.RBS_ELEC_PANEL_NAME)?.AsString();
            return string.IsNullOrEmpty(s) ? panel.Name : s;
        }

        public static List<FamilyInstance> AllPanels(Document doc)
            => new FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                .WhereElementIsNotElementType()
                .OfType<FamilyInstance>()
                .Where(f => f.MEPModel != null)
                .OrderBy(PanelName, StringComparer.OrdinalIgnoreCase)
                .ToList();

        public static int CircuitNo(ElectricalSystem c)
            => int.TryParse((c.CircuitNumber ?? "").Split(',')[0], out int n) ? n : 99999;

        public static List<ElectricalSystem> AllCircuits(Document doc)
            => new FilteredElementCollector(doc)
                .OfClass(typeof(ElectricalSystem))
                .Cast<ElectricalSystem>()
                .OrderBy(c => string.IsNullOrEmpty(c.PanelName) ? "~unassigned" : c.PanelName.ToLowerInvariant())
                .ThenBy(CircuitNo)
                .ThenBy(c => c.CircuitNumber ?? "")
                .ToList();

        public static string CircuitLabel(ElectricalSystem c)
        {
            int n = 0;
            try { n = c.Elements.Size; } catch (Exception) { }
            string cls = "-";
            try { if (!string.IsNullOrEmpty(c.LoadClassifications)) cls = c.LoadClassifications; } catch (Exception) { }
            string stype = c.SystemType.ToString().Replace("Circuit", "");
            string panel = string.IsNullOrEmpty(c.PanelName) ? "<no panel>" : c.PanelName;
            string num = string.IsNullOrEmpty(c.CircuitNumber) ? "-" : c.CircuitNumber;
            return $"{panel}   |   Ckt {num}   |   {c.LoadName ?? ""}   |   {n} elem   |   {cls}   |   {stype}";
        }

        public static ElementSet MakeSet(IEnumerable<Element> elements)
        {
            var s = new ElementSet();
            foreach (Element e in elements) s.Insert(e);
            return s;
        }

        /// <summary>Runs one undoable transaction; rolls back if anything throws.</summary>
        public static void InTx(Document doc, string name, Action action)
        {
            using (var t = new Transaction(doc, "Circuit Manager - " + name))
            {
                t.Start();
                FailureHandlingOptions o = t.GetFailureHandlingOptions();
                o.SetClearAfterRollback(true);
                t.SetFailureHandlingOptions(o);
                try
                {
                    action();
                    t.Commit();
                }
                catch
                {
                    if (t.HasStarted() && !t.HasEnded()) t.RollBack();
                    throw;
                }
            }
        }
    }
}
