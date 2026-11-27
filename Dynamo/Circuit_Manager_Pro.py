# -*- coding: utf-8 -*-
# =============================================================================
# CIRCUIT MANAGER PRO  -  Dynamo Python Script node for Revit
# -----------------------------------------------------------------------------
# Opens one dialog with three tabs:
#
#   1. CREATE   Pick many fixtures / devices and give EACH ONE its own power
#               circuit (one circuit per element), optionally assigned straight
#               to a panel. Elements already on a power circuit can be skipped.
#   2. MANAGE   List every circuit (filter by panel / unassigned / text).
#               Multi-select circuits and in bulk:
#                 - move them to another panel
#                 - disconnect them from their panel
#                 - delete them
#               Circuits can also be selected by picking elements in the model.
#   3. EDIT     Choose one circuit, see the elements on it, then
#                 - remove selected elements from the circuit
#                 - add more elements to the circuit (pick in the model)
#   4. CHECK    Load classification check for one panel: lists every load
#               under the load classification its connector REALLY uses and
#               explains why a panel reports several classifications although
#               they all look the same (duplicate names, parameter not linked
#               to the connector, text parameter, several connectors,
#               downstream panels, spares). Selected loads can be set to one
#               classification in one click.
#
# Every action runs in its own Revit transaction, so each one can be undone
# separately with Ctrl+Z after the dialog is closed.
#
# HOW TO USE
#   Paste this into a Dynamo "Python Script" node (engine CPython3 or
#   PythonNet3; IronPython2 also works). Connect a Boolean node to IN[0]
#   (True = open the dialog) and run the graph. OUT returns the action log.
#   Or simply open Circuit_Manager_Pro.dyn, which already contains all this.
#
# Revit 2022 - 2027. Element ids are read with eid_val() because
# ElementId.IntegerValue was removed in Revit 2026.
# =============================================================================

import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("RevitServices")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xml")

import System
from System.Collections.Generic import List

from Autodesk.Revit import DB
from Autodesk.Revit.DB.Electrical import ElectricalSystem, ElectricalSystemType
from Autodesk.Revit.UI.Selection import ObjectType

from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

from System.Windows import Visibility
from System.Windows import FontWeights
from System.Windows.Controls import ComboBoxItem, ListBoxItem
from System.Windows.Media import BrushConverter, Brushes
from System.Windows.Markup import XamlReader

doc = DocumentManager.Instance.CurrentDBDocument
uiapp = DocumentManager.Instance.CurrentUIApplication
uidoc = uiapp.ActiveUIDocument


# =============================================================================
# Revit helpers
# =============================================================================
def eid_val(eid):
    """Integer value of an ElementId on every Revit version."""
    try:
        return int(eid.Value)          # Revit 2024+
    except Exception:
        return int(eid.IntegerValue)   # Revit 2023 and older


def to_eid(value):
    try:
        return DB.ElementId(System.Int64(value))
    except Exception:
        return DB.ElementId(int(value))


def get_el(value):
    return doc.GetElement(to_eid(value))


# Categories that can be put on a power circuit
CIRCUITABLE_BICS = [
    DB.BuiltInCategory.OST_LightingFixtures,
    DB.BuiltInCategory.OST_LightingDevices,
    DB.BuiltInCategory.OST_ElectricalFixtures,
    DB.BuiltInCategory.OST_ElectricalEquipment,
    DB.BuiltInCategory.OST_MechanicalEquipment,
    DB.BuiltInCategory.OST_SpecialityEquipment,
    DB.BuiltInCategory.OST_DataDevices,
    DB.BuiltInCategory.OST_CommunicationDevices,
    DB.BuiltInCategory.OST_FireAlarmDevices,
    DB.BuiltInCategory.OST_SecurityDevices,
    DB.BuiltInCategory.OST_NurseCallDevices,
    DB.BuiltInCategory.OST_TelephoneDevices,
]
CIRCUITABLE_CAT_IDS = set()
for _bic in CIRCUITABLE_BICS:
    try:
        CIRCUITABLE_CAT_IDS.add(eid_val(DB.ElementId(_bic)))
    except Exception:
        pass


def has_electrical_connector(el):
    try:
        mep = el.MEPModel
        if mep is None or mep.ConnectorManager is None:
            return False
        for c in mep.ConnectorManager.Connectors:
            if c.Domain == DB.Domain.DomainElectrical:
                return True
    except Exception:
        pass
    return False


def is_circuitable(el):
    if not isinstance(el, DB.FamilyInstance) or el.Category is None:
        return False
    if eid_val(el.Category.Id) not in CIRCUITABLE_CAT_IDS:
        return False
    return has_electrical_connector(el)


def element_systems(el):
    """Electrical systems (circuits) an element belongs to."""
    out = []
    try:
        mep = el.MEPModel
        try:
            systems = mep.GetElectricalSystems()       # Revit 2021+
        except Exception:
            systems = mep.ElectricalSystems             # older API
        if systems:
            for s in systems:
                out.append(s)
    except Exception:
        pass
    return out


def power_systems(el):
    return [s for s in element_systems(el)
            if s.SystemType == ElectricalSystemType.PowerCircuit]


def element_label(el):
    try:
        cat = el.Category.Name if el.Category else "?"
        fam = el.Symbol.FamilyName if isinstance(el, DB.FamilyInstance) else ""
        typ = el.Name
        mark = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        mark = mark.AsString() if mark and mark.AsString() else ""
        txt = u"{0}  :  {1} - {2}".format(cat, fam, typ)
        if mark:
            txt += u"   [Mark {0}]".format(mark)
        return txt + u"   (id {0})".format(eid_val(el.Id))
    except Exception:
        return u"Element {0}".format(eid_val(el.Id))


def panel_name(panel):
    try:
        p = panel.get_Parameter(DB.BuiltInParameter.RBS_ELEC_PANEL_NAME)
        if p and p.AsString():
            return p.AsString()
    except Exception:
        pass
    return panel.Name


def all_panels():
    panels = []
    col = (DB.FilteredElementCollector(doc)
           .OfCategory(DB.BuiltInCategory.OST_ElectricalEquipment)
           .WhereElementIsNotElementType())
    for e in col:
        if isinstance(e, DB.FamilyInstance) and e.MEPModel is not None:
            panels.append(e)
    panels.sort(key=lambda p: panel_name(p).lower())
    return panels


def all_circuits():
    col = DB.FilteredElementCollector(doc).OfClass(ElectricalSystem)
    circuits = [c for c in col]

    def key(c):
        pn = (c.PanelName or u"~unassigned").lower()
        num = c.CircuitNumber or u""
        try:
            n = int(num.split(",")[0])
        except Exception:
            n = 99999
        return (pn, n, num)
    circuits.sort(key=key)
    return circuits


def circuit_label(c):
    try:
        n = c.Elements.Size
    except Exception:
        n = 0
    try:
        cls = c.LoadClassifications or u"-"
    except Exception:
        cls = u"-"
    try:
        stype = str(c.SystemType).replace("Circuit", "")
    except Exception:
        stype = u""
    panel = c.PanelName if c.PanelName else u"<no panel>"
    num = c.CircuitNumber if c.CircuitNumber else u"-"
    load = c.LoadName if c.LoadName else u""
    return u"{0}   |   Ckt {1}   |   {2}   |   {3} elem   |   {4}   |   {5}".format(
        panel, num, load, n, cls, stype)


def pick_elements(prompt):
    """Let the user pick elements; returns [] on Esc."""
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, prompt)
    except Exception:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def make_set(elements):
    es = DB.ElementSet()
    for e in elements:
        es.Insert(e)
    return es


class Tx(object):
    """with Tx("name"): ...  -> one undoable Revit transaction."""

    def __init__(self, name):
        self.t = DB.Transaction(doc, "Circuit Manager - " + name)

    def __enter__(self):
        self.t.Start()
        opts = self.t.GetFailureHandlingOptions()
        opts.SetClearAfterRollback(True)
        self.t.SetFailureHandlingOptions(opts)
        return self.t

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.t.Commit()
        else:
            self.t.RollBack()
        return False


# =============================================================================
# Load classification check
# -----------------------------------------------------------------------------
# Why a panel can report several load classifications although every fixture
# "shows the same one":
#   a) The project holds two or more load classification ITEMS with the same
#      (or nearly the same - extra space, different case, non-breaking space)
#      name. Properties shows only the name, Revit counts each item separately.
#      Families loaded from other templates often bring such a duplicate.
#   b) The parameter you edited is not the one linked to the family's
#      electrical connector (the connector has a fixed value inside the
#      family, or is linked to another parameter, or the parameter is text).
#   c) A family has more than one power connector, with different values.
#   d) The panel feeds another panel / transformer whose loads are different.
#   e) A spare circuit on the panel carries another classification.
# run_lc_check() finds every one of these for one panel.
# =============================================================================
try:
    from Autodesk.Revit.DB.Electrical import ElectricalLoadClassification
except Exception:
    ElectricalLoadClassification = None

try:
    _LC_SPEC = DB.SpecTypeId.Reference.LoadClassification
except Exception:
    _LC_SPEC = None

_BIP_LC = getattr(DB.BuiltInParameter, "RBS_ELEC_LOAD_CLASSIFICATION", None)
_FAMILY_CACHE = {}
try:
    _ID_TYPES = (int, long)     # IronPython 2 may return long for big ids
except NameError:
    _ID_TYPES = (int,)


def lc_visible(name):
    """Name in quotes with hidden differences made visible:
    a space at either end -> a dot, any non-ASCII character -> \\uXXXX."""
    if name is None:
        return u"<none>"
    chars = []
    for ch in name:
        if ord(ch) > 126 or ord(ch) < 32:
            chars.append(u"\\u%04X" % ord(ch))
        else:
            chars.append(ch)
    s = u"".join(chars)
    core = s.strip(u" ")
    lead = len(s) - len(s.lstrip(u" "))
    trail = len(s) - len(s.rstrip(u" "))
    out = u"'" + u"·" * lead + core + u"·" * trail + u"'"
    if u"  " in core:
        out += u" [double space]"
    return out


def norm_name(name):
    return u" ".join((name or u"").replace(u" ", u" ").split()).lower()


def all_load_classifications():
    out = {}
    if ElectricalLoadClassification is None:
        return out
    for lc in DB.FilteredElementCollector(doc).OfClass(ElectricalLoadClassification):
        out[eid_val(lc.Id)] = lc
    return out


def duplicate_groups(lcs):
    """{id: [other ids whose name is the same after trimming / ignoring case]}"""
    by_name = {}
    for v, lc in lcs.items():
        by_name.setdefault(norm_name(lc.Name), []).append(v)
    out = {}
    for ids in by_name.values():
        if len(ids) > 1:
            for v in ids:
                out[v] = [x for x in ids if x != v]
    return out


def param_owners(el):
    owners = [("instance", el)]
    try:
        if isinstance(el, DB.FamilyInstance) and el.Symbol is not None:
            owners.append(("type", el.Symbol))
    except Exception:
        pass
    return owners


def is_lc_param(p, lcs):
    try:
        if p is None or p.StorageType != DB.StorageType.ElementId:
            return False
    except Exception:
        return False
    if _LC_SPEC is not None:
        try:
            if p.Definition.GetDataType() == _LC_SPEC:
                return True
        except Exception:
            pass
    try:
        return eid_val(p.AsElementId()) in lcs
    except Exception:
        return False


def lc_params(el, lcs):
    """[(where, Parameter)] - every load classification parameter on the
    element ("instance") and on its type ("type")."""
    found = []
    for where, owner in param_owners(el):
        try:
            params = owner.Parameters
        except Exception:
            continue
        for p in params:
            if is_lc_param(p, lcs):
                found.append((where, p))
    return found


def lc_text_params(el):
    """Text parameters whose name looks like 'load classification'."""
    found = []
    for where, owner in param_owners(el):
        try:
            params = owner.Parameters
        except Exception:
            continue
        for p in params:
            try:
                if p.StorageType != DB.StorageType.String:
                    continue
                n = p.Definition.Name.lower()
                if "load" in n and "class" in n:
                    found.append((where, p.Definition.Name, p.AsString() or u""))
            except Exception:
                pass
    return found


def family_connectors(el):
    """Read the element's family file: [(classification name, linked family
    parameter name or None)] for every power connector. None = unreadable."""
    try:
        fam = el.Symbol.Family
    except Exception:
        return None
    key = eid_val(fam.Id)
    if key in _FAMILY_CACHE:
        return _FAMILY_CACHE[key]
    result = None
    try:
        if fam.IsInPlace or not fam.IsEditable or _BIP_LC is None:
            _FAMILY_CACHE[key] = None
            return None
    except Exception:
        pass
    already_open = [d for d in doc.Application.Documents]
    fdoc = None
    try:
        fdoc = doc.EditFamily(fam)
        result = []
        for ce in DB.FilteredElementCollector(fdoc).OfClass(DB.ConnectorElement):
            try:
                if ce.Domain != DB.Domain.DomainElectrical:
                    continue
            except Exception:
                continue
            p = ce.get_Parameter(_BIP_LC)
            if p is None:
                continue                  # data / comms connector
            lce = fdoc.GetElement(p.AsElementId())
            name = lce.Name if lce is not None else None
            assoc = None
            try:
                fp = fdoc.FamilyManager.GetAssociatedFamilyParameter(p)
                if fp is not None:
                    assoc = fp.Definition.Name
            except Exception:
                pass
            result.append((name, assoc))
    except Exception:
        result = None
    finally:
        if fdoc is not None and not any(d.Equals(fdoc) for d in already_open):
            try:
                fdoc.Close(False)
            except Exception:
                pass
    _FAMILY_CACHE[key] = result
    return result


def resolve_name(name, lcs):
    """Project id for a classification name set inside a family, or '?name'
    when the project has none or several classifications with that name."""
    if name is None:
        return None
    exact = [v for v, lc in lcs.items() if lc.Name == name]
    if len(exact) == 1:
        return exact[0]
    return u"?" + name


def is_electrical_equipment(el):
    try:
        return eid_val(el.Category.Id) == eid_val(
            DB.ElementId(DB.BuiltInCategory.OST_ElectricalEquipment))
    except Exception:
        return False


def circuits_by_panel():
    out = {}
    for c in DB.FilteredElementCollector(doc).OfClass(ElectricalSystem):
        try:
            be = c.BaseEquipment
        except Exception:
            be = None
        if be is not None:
            out.setdefault(eid_val(be.Id), []).append(c)
    return out


def analyse_lc(el, ckt, lcs, deep, spare=False):
    """One row of the check for a load (or a spare circuit)."""
    row = {"id": eid_val(el.Id), "ckt": ckt, "kind": "spare" if spare else "load",
           "label": u"Spare / space circuit" if spare else element_label(el),
           "eff": [], "how": [], "problems": [], "drive": None}
    pvals = []
    for where, p in lc_params(el, lcs):
        v = eid_val(p.AsElementId())
        pvals.append((where, p.Definition.Name, v if v in lcs else None))

    fam = None
    if deep and not spare and isinstance(el, DB.FamilyInstance):
        fam = family_connectors(el)

    if fam:
        row["drive"] = [a for _, a in fam if a]
        for name, assoc in fam:
            if assoc:
                match = [pv for pv in pvals if pv[1] == assoc]
                if match:
                    where, pname, v = match[0]
                    row["eff"].append(v)
                    row["how"].append(u"connector uses {0} parameter '{1}'".format(where, pname))
                    if v is None:
                        row["problems"].append(u"parameter '{0}' is empty".format(pname))
                else:
                    row["eff"].append(None)
                    row["how"].append(u"connector uses parameter '{0}'".format(assoc))
                    row["problems"].append(
                        u"connector is linked to '{0}', which was not found on the element".format(assoc))
            else:
                row["eff"].append(resolve_name(name, lcs))
                row["how"].append(u"fixed inside the family: {0}".format(lc_visible(name)))
        not_driving = [pv[1] for pv in pvals if pv[1] not in row["drive"]]
        if not_driving:
            row["problems"].append(
                u"parameter {0} is NOT linked to the electrical connector - changing it "
                u"does nothing. In the family, link the connector's Load Classification "
                u"to it.".format(u", ".join(u"'%s'" % n for n in not_driving)))
        if len(fam) > 1 and len(set(row["eff"])) > 1:
            row["problems"].append(
                u"family has {0} power connectors with different load classifications".format(len(fam)))
    elif pvals:
        for where, pname, v in pvals:
            row["eff"].append(v)
            row["how"].append(u"{0} parameter '{1}'".format(where, pname))
            if v is None:
                row["problems"].append(u"parameter '{0}' is empty".format(pname))
        if len(set(pv[2] for pv in pvals)) > 1:
            row["problems"].append(
                u"{0} load classification parameters with different values - only one drives "
                u"the connector (tick 'Read the families' to see which)".format(len(pvals)))
    elif spare:
        row["how"].append(u"no load classification")
    else:
        row["eff"].append(None)
        if deep:
            row["how"].append(u"family could not be read (in-place or protected family)")
        else:
            row["how"].append(u"set inside the family - tick 'Read the families' to see it")

    if not pvals:
        for where, pname, text in lc_text_params(el):
            row["problems"].append(
                u"{0} parameter '{1}' = '{2}' is only TEXT - it is not the connector's "
                u"load classification".format(where, pname, text))
    return row


def run_lc_check(panel, downstream, deep):
    lcs = all_load_classifications()
    dups = duplicate_groups(lcs)
    by_panel = circuits_by_panel()
    rows, circuit_values, visited = [], {}, set()

    def ckt_key(c):
        try:
            return int((c.CircuitNumber or u"0").split(",")[0])
        except Exception:
            return 99999

    def walk(equip, path):
        if eid_val(equip.Id) in visited:
            return
        visited.add(eid_val(equip.Id))
        for c in sorted(by_panel.get(eid_val(equip.Id), []), key=ckt_key):
            try:
                if c.SystemType != ElectricalSystemType.PowerCircuit:
                    continue
            except Exception:
                continue
            ckt = u"{0} / Ckt {1}".format(path, c.CircuitNumber or u"-")
            try:
                val = c.LoadClassifications or u""
            except Exception:
                val = u""
            if val:
                circuit_values[val] = circuit_values.get(val, 0) + 1
            elems = [e for e in c.Elements]
            if not elems:
                rows.append(analyse_lc(c, ckt, lcs, deep, spare=True))
                continue
            for e in elems:
                if is_electrical_equipment(e) and by_panel.get(eid_val(e.Id)):
                    if downstream:
                        walk(e, path + u" > " + panel_name(e))
                    else:
                        rows.append({
                            "id": eid_val(e.Id), "ckt": ckt, "kind": "feeder",
                            "label": element_label(e), "eff": [], "drive": None,
                            "how": [u"feeds panel '{0}'".format(panel_name(e))],
                            "problems": [u"Revit adds the loads of '{0}' (and their load "
                                         u"classifications) to this panel - tick 'Include "
                                         u"downstream panels' to check them".format(panel_name(e))]})
                    continue
                rows.append(analyse_lc(e, ckt, lcs, deep))

    walk(panel, panel_name(panel))

    groups, order, usage = {}, [], {}
    for r in rows:
        keys = []
        for k in (r["eff"] or ["__info__"]):
            if k not in keys:
                keys.append(k)
        for k in keys:
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(r)
            if isinstance(k, _ID_TYPES):
                usage[k] = usage.get(k, 0) + 1
    real = [k for k in order if k != "__info__"]
    real.sort(key=lambda k: -len(groups[k]))
    if "__info__" in groups:
        real.append("__info__")

    def key_label(k):
        if k == "__info__":
            return u"Not counted as a load (spare circuits / feeders)"
        if k is None:
            return u"UNKNOWN or EMPTY load classification"
        if isinstance(k, _ID_TYPES):
            txt = u"{0}   (id {1})".format(lc_visible(lcs[k].Name), k)
            if k in dups:
                txt += u"   !! SAME NAME AS id {0}".format(u", ".join(str(x) for x in dups[k]))
            return txt
        name = k[1:]
        n = len([v for v, lc in lcs.items() if lc.Name == name])
        if n > 1:
            return u"{0} set inside families - {1} project classifications have this name".format(
                lc_visible(name), n)
        return u"{0} set inside families - no project classification has exactly this name".format(
            lc_visible(name))

    counted = [k for k in real if k != "__info__"]
    n_loads = len([r for r in rows if r["kind"] == "load"])
    n_prob = len([r for r in rows if r["problems"]])
    lines = []
    ok = (len(counted) == 1 and isinstance(counted[0], int) and n_prob == 0)
    pname = panel_name(panel)
    if not rows:
        lines.append(u"Panel '{0}' has no circuits with loads.".format(pname))
        ok = None
    elif ok:
        lines.append(u"OK - all {0} load(s) on '{1}' use ONE load classification: {2}".format(
            n_loads, pname, key_label(counted[0])))
    else:
        lines.append(u"PROBLEM - {0} different load classification(s) feed '{1}' ({2} loads):".format(
            len(counted), pname, n_loads))
        for k in counted:
            lines.append(u"   - {0}  ->  {1} element(s)".format(key_label(k), len(groups[k])))
        same = [k for k in counted if isinstance(k, _ID_TYPES) and k in dups
                and any(o in counted for o in dups[k])]
        if same:
            lines.append(u"CAUSE: the project has load classifications with the SAME NAME. They look "
                         u"identical in Properties, but they are different items, so Revit lists them "
                         u"separately. Fix: select all rows, choose ONE classification under Fix, press "
                         u"Apply, then delete the spare one in Manage > MEP Settings > Load "
                         u"Classifications.")
        if any(isinstance(k, _ID_TYPES) and k in dups for k in counted) and not same:
            lines.append(u"Note: a classification used here has a twin with the same name in the project.")
        if n_prob:
            lines.append(u"{0} row(s) have warnings (red) - read them in the list.".format(n_prob))
        if not deep:
            lines.append(u"Tip: tick 'Read the families' - the value you see in Properties is not "
                         u"always the one the electrical connector uses.")
    if circuit_values:
        vals = sorted(circuit_values.items(), key=lambda kv: -kv[1])[:8]
        lines.append(u"Revit's circuit 'Load Classification' values: " + u";  ".join(
            u"{0} ({1} ckt)".format(lc_visible(v), n) for v, n in vals))

    return {"panel": pname, "rows": rows, "order": real, "groups": groups,
            "labels": dict((k, key_label(k)) for k in real), "usage": usage,
            "lcs": lcs, "dups": dups, "verdict": lines, "ok": ok, "deep": deep}

BRUSH = BrushConverter()


# =============================================================================
# Dialog (WPF / XAML) - raised "3D" cards, glossy buttons, spinning 3D cube
# =============================================================================
XAML = u"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Circuit Manager Pro" Width="1100" Height="780"
        MinWidth="860" MinHeight="600"
        WindowStartupLocation="CenterScreen" FontFamily="Segoe UI" FontSize="13"
        Background="#E9EEF5">
  <Window.Resources>
    <DropShadowEffect x:Key="CardShadow" BlurRadius="18" ShadowDepth="5"
                      Direction="270" Opacity="0.28" Color="#1B2A41"/>
    <DropShadowEffect x:Key="BtnShadow" BlurRadius="8" ShadowDepth="3"
                      Direction="270" Opacity="0.40" Color="#0B1626"/>

    <Style x:Key="Card" TargetType="Border">
      <Setter Property="Background">
        <Setter.Value>
          <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="#FFFFFF" Offset="0"/>
            <GradientStop Color="#F3F6FB" Offset="1"/>
          </LinearGradientBrush>
        </Setter.Value>
      </Setter>
      <Setter Property="BorderBrush" Value="#D3DCE8"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="12"/>
      <Setter Property="Padding" Value="14"/>
      <Setter Property="Margin" Value="8"/>
      <Setter Property="Effect" Value="{StaticResource CardShadow}"/>
    </Style>

    <Style x:Key="Btn3D" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Padding" Value="16,8"/>
      <Setter Property="Margin" Value="4"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid x:Name="root" RenderTransformOrigin="0.5,0.5">
              <Grid.RenderTransform>
                <TranslateTransform Y="0"/>
              </Grid.RenderTransform>
              <Border x:Name="base" CornerRadius="9" Margin="0,3,0,0"
                      Background="#16325F"/>
              <Border x:Name="face" CornerRadius="9" Margin="0,0,0,3"
                      Effect="{StaticResource BtnShadow}">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                    <GradientStop Color="#6FA2FF" Offset="0"/>
                    <GradientStop Color="#2F6FE4" Offset="0.55"/>
                    <GradientStop Color="#2457B8" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <Grid>
                  <Border CornerRadius="9,9,30,30" Height="12"
                          VerticalAlignment="Top" Margin="3,2,3,0">
                    <Border.Background>
                      <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                        <GradientStop Color="#80FFFFFF" Offset="0"/>
                        <GradientStop Color="#00FFFFFF" Offset="1"/>
                      </LinearGradientBrush>
                    </Border.Background>
                  </Border>
                  <ContentPresenter Margin="{TemplateBinding Padding}"
                                    HorizontalAlignment="Center"
                                    VerticalAlignment="Center"/>
                </Grid>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="-2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Opacity" Value="0.95"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Margin" Value="0,2,0,1"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="root" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="BtnDanger" TargetType="Button" BasedOn="{StaticResource Btn3D}">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid x:Name="root">
              <Grid.RenderTransform>
                <TranslateTransform Y="0"/>
              </Grid.RenderTransform>
              <Border CornerRadius="9" Margin="0,3,0,0" Background="#6B1414"/>
              <Border x:Name="face" CornerRadius="9" Margin="0,0,0,3"
                      Effect="{StaticResource BtnShadow}">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                    <GradientStop Color="#FF8A80" Offset="0"/>
                    <GradientStop Color="#E0453A" Offset="0.55"/>
                    <GradientStop Color="#B3261E" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <ContentPresenter Margin="{TemplateBinding Padding}"
                                  HorizontalAlignment="Center"
                                  VerticalAlignment="Center"/>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="-2"/></Setter.Value></Setter>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Margin" Value="0,2,0,1"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="root" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="BtnGreen" TargetType="Button" BasedOn="{StaticResource Btn3D}">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid x:Name="root">
              <Grid.RenderTransform>
                <TranslateTransform Y="0"/>
              </Grid.RenderTransform>
              <Border CornerRadius="9" Margin="0,3,0,0" Background="#0F4D2C"/>
              <Border x:Name="face" CornerRadius="9" Margin="0,0,0,3"
                      Effect="{StaticResource BtnShadow}">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                    <GradientStop Color="#7EE2A8" Offset="0"/>
                    <GradientStop Color="#22A55B" Offset="0.55"/>
                    <GradientStop Color="#17803F" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <ContentPresenter Margin="{TemplateBinding Padding}"
                                  HorizontalAlignment="Center"
                                  VerticalAlignment="Center"/>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="-2"/></Setter.Value></Setter>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Margin" Value="0,2,0,1"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="root" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="H2" TargetType="TextBlock">
      <Setter Property="FontSize" Value="15"/>
      <Setter Property="FontWeight" Value="Bold"/>
      <Setter Property="Foreground" Value="#1B2A41"/>
      <Setter Property="Margin" Value="0,0,0,8"/>
    </Style>
    <Style x:Key="Hint" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#5A6B82"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="Margin" Value="0,0,0,8"/>
    </Style>
    <Style TargetType="ListBox">
      <Setter Property="BorderBrush" Value="#C9D4E3"/>
      <Setter Property="Background" Value="#FBFCFE"/>
      <Setter Property="FontFamily" Value="Consolas, Segoe UI"/>
      <Setter Property="FontSize" Value="12.5"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Padding" Value="6,4"/>
      <Setter Property="Margin" Value="4"/>
      <Setter Property="MinWidth" Value="200"/>
    </Style>
    <Style TargetType="TabItem">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TabItem">
            <Border x:Name="b" CornerRadius="10,10,0,0" Margin="2,0"
                    Padding="18,8" Background="#C8D3E3">
              <ContentPresenter ContentSource="Header"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="b" Property="Background">
                  <Setter.Value>
                    <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                      <GradientStop Color="#FFFFFF" Offset="0"/>
                      <GradientStop Color="#F3F6FB" Offset="1"/>
                    </LinearGradientBrush>
                  </Setter.Value>
                </Setter>
                <Setter Property="FontWeight" Value="Bold"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="120"/>
    </Grid.RowDefinitions>

    <!-- ===== Header with spinning 3D cube ===== -->
    <Border Grid.Row="0" Margin="8,8,8,4" CornerRadius="14" Padding="14,8"
            Effect="{StaticResource CardShadow}">
      <Border.Background>
        <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
          <GradientStop Color="#0F2447" Offset="0"/>
          <GradientStop Color="#1E4FA3" Offset="0.6"/>
          <GradientStop Color="#2F7BEA" Offset="1"/>
        </LinearGradientBrush>
      </Border.Background>
      <DockPanel>
        <Viewport3D DockPanel.Dock="Left" Width="70" Height="64" ClipToBounds="False">
          <Viewport3D.Camera>
            <PerspectiveCamera Position="3.2,2.6,4.2" LookDirection="-3.2,-2.6,-4.2"
                               UpDirection="0,1,0" FieldOfView="45"/>
          </Viewport3D.Camera>
          <ModelVisual3D>
            <ModelVisual3D.Content>
              <Model3DGroup>
                <AmbientLight Color="#555555"/>
                <DirectionalLight Color="#FFFFFF" Direction="-1,-1.5,-2"/>
                <GeometryModel3D>
                  <GeometryModel3D.Geometry>
                    <MeshGeometry3D
                      Positions="-1,-1,1 1,-1,1 1,1,1 -1,1,1  1,-1,-1 -1,-1,-1 -1,1,-1 1,1,-1  -1,-1,-1 -1,-1,1 -1,1,1 -1,1,-1  1,-1,1 1,-1,-1 1,1,-1 1,1,1  -1,1,1 1,1,1 1,1,-1 -1,1,-1  -1,-1,-1 1,-1,-1 1,-1,1 -1,-1,1"
                      TriangleIndices="0 1 2 0 2 3  4 5 6 4 6 7  8 9 10 8 10 11  12 13 14 12 14 15  16 17 18 16 18 19  20 21 22 20 22 23"/>
                  </GeometryModel3D.Geometry>
                  <GeometryModel3D.Material>
                    <DiffuseMaterial>
                      <DiffuseMaterial.Brush>
                        <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
                          <GradientStop Color="#FFD54F" Offset="0"/>
                          <GradientStop Color="#FF8F00" Offset="1"/>
                        </LinearGradientBrush>
                      </DiffuseMaterial.Brush>
                    </DiffuseMaterial>
                  </GeometryModel3D.Material>
                  <GeometryModel3D.Transform>
                    <RotateTransform3D>
                      <RotateTransform3D.Rotation>
                        <AxisAngleRotation3D x:Name="cubeRot" Axis="0.3,1,0.2" Angle="0"/>
                      </RotateTransform3D.Rotation>
                    </RotateTransform3D>
                  </GeometryModel3D.Transform>
                </GeometryModel3D>
              </Model3DGroup>
            </ModelVisual3D.Content>
          </ModelVisual3D>
          <Viewport3D.Triggers>
            <EventTrigger RoutedEvent="Viewport3D.Loaded">
              <BeginStoryboard>
                <Storyboard>
                  <DoubleAnimation Storyboard.TargetName="cubeRot"
                                   Storyboard.TargetProperty="Angle"
                                   From="0" To="360" Duration="0:0:8"
                                   RepeatBehavior="Forever"/>
                </Storyboard>
              </BeginStoryboard>
            </EventTrigger>
          </Viewport3D.Triggers>
        </Viewport3D>
        <StackPanel VerticalAlignment="Center" Margin="12,0,0,0">
          <TextBlock Text="&#x26A1; CIRCUIT MANAGER PRO" Foreground="White"
                     FontSize="22" FontWeight="Bold">
            <TextBlock.Effect>
              <DropShadowEffect BlurRadius="6" ShadowDepth="2" Opacity="0.6"/>
            </TextBlock.Effect>
          </TextBlock>
          <TextBlock x:Name="txtDoc" Foreground="#CFE0FF" FontSize="12"
                     Text="Create, move, edit and remove electrical circuits in bulk"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <!-- ===== Tabs ===== -->
    <TabControl x:Name="tabs" Grid.Row="1" Margin="8,4,8,0" Background="Transparent"
                BorderThickness="0" Padding="0">

      <!-- ===== TAB 1 : CREATE ===== -->
      <TabItem Header="&#x2795;  Create circuits">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="320"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}"
                         x:Name="lblFixtures" Text="Fixtures to circuit (0)"/>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource Hint}"
                         Text="Each element in this list gets its OWN circuit. Pick in the model, then press Finish in Revit's options bar."/>
              <WrapPanel DockPanel.Dock="Bottom" Margin="0,8,0,0">
                <Button x:Name="btnPickFix" Style="{StaticResource Btn3D}" Content="&#x1F5B1; Pick fixtures..."/>
                <Button x:Name="btnRemFix" Style="{StaticResource Btn3D}" Content="Remove selected from list"/>
                <Button x:Name="btnClearFix" Style="{StaticResource BtnDanger}" Content="Clear list"/>
              </WrapPanel>
              <ListBox x:Name="lstFixtures" SelectionMode="Extended"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <StackPanel>
              <TextBlock Style="{StaticResource H2}" Text="Options"/>
              <TextBlock Style="{StaticResource Hint}" Text="Assign the new circuits to panel:"/>
              <ComboBox x:Name="cmbCreatePanel"/>
              <CheckBox x:Name="chkSkip" Margin="4,12,4,4" IsChecked="True"
                        Content="Skip elements already on a power circuit"/>
              <TextBlock Style="{StaticResource Hint}" Margin="4,12,4,4"
                         Text="The panel's distribution system must match the voltage / poles of the fixtures, otherwise the circuit is created without a panel and the reason is logged below."/>
              <Button x:Name="btnCreate" Style="{StaticResource BtnGreen}" Margin="4,18,4,4"
                      Content="&#x26A1; Create one circuit per element" FontSize="14"/>
            </StackPanel>
          </Border>
        </Grid>
      </TabItem>

      <!-- ===== TAB 2 : MANAGE ===== -->
      <TabItem Header="&#x21C4;  Move / remove circuits">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="320"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}"
                         x:Name="lblCircuits" Text="Circuits"/>
              <WrapPanel DockPanel.Dock="Top">
                <TextBlock Text="Panel:" VerticalAlignment="Center" Margin="0,0,4,0"/>
                <ComboBox x:Name="cmbFilterPanel"/>
                <TextBlock Text="Search:" VerticalAlignment="Center" Margin="10,0,4,0"/>
                <TextBox x:Name="txtSearch" Width="180" Padding="4" Margin="4"/>
              </WrapPanel>
              <WrapPanel DockPanel.Dock="Bottom" Margin="0,8,0,0">
                <Button x:Name="btnSelAll" Style="{StaticResource Btn3D}" Content="Select all"/>
                <Button x:Name="btnSelNone" Style="{StaticResource Btn3D}" Content="Select none"/>
                <Button x:Name="btnPickCkt" Style="{StaticResource Btn3D}" Content="&#x1F5B1; Select by picking elements..."/>
                <Button x:Name="btnRefresh" Style="{StaticResource Btn3D}" Content="&#x21BB; Refresh"/>
              </WrapPanel>
              <ListBox x:Name="lstCircuits" SelectionMode="Extended" Margin="0,6,0,0"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <StackPanel>
              <TextBlock Style="{StaticResource H2}" Text="Bulk actions"/>
              <TextBlock Style="{StaticResource Hint}" x:Name="lblSelCount" Text="0 circuits selected"/>
              <TextBlock Style="{StaticResource Hint}" Text="Move selected circuits to panel:"/>
              <ComboBox x:Name="cmbMovePanel"/>
              <Button x:Name="btnMove" Style="{StaticResource BtnGreen}" Content="&#x21C4; Move to panel"/>
              <Separator Margin="0,14"/>
              <Button x:Name="btnDisconnect" Style="{StaticResource Btn3D}" Content="Disconnect from panel"/>
              <Button x:Name="btnEditSel" Style="{StaticResource Btn3D}" Content="Edit elements of selected circuit"/>
              <Separator Margin="0,14"/>
              <Button x:Name="btnDelete" Style="{StaticResource BtnDanger}" Content="&#x2716; Delete selected circuits"/>
            </StackPanel>
          </Border>
        </Grid>
      </TabItem>

      <!-- ===== TAB 3 : EDIT ===== -->
      <TabItem Header="&#x270E;  Add / remove elements">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="320"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}" Text="Circuit"/>
              <ComboBox DockPanel.Dock="Top" x:Name="cmbEditCircuit" Margin="0,0,0,8"/>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource Hint}"
                         x:Name="lblEditElems" Text="Elements on this circuit"/>
              <ListBox x:Name="lstEditElems" SelectionMode="Extended"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <StackPanel>
              <TextBlock Style="{StaticResource H2}" Text="Actions"/>
              <Button x:Name="btnAddElems" Style="{StaticResource BtnGreen}" Content="&#x2795; Add elements (pick)..."/>
              <Button x:Name="btnRemElems" Style="{StaticResource BtnDanger}" Content="&#x2796; Remove selected elements"/>
              <TextBlock Style="{StaticResource Hint}" Margin="4,14,4,4"
                         Text="Revit will not remove the last element of a circuit - delete the circuit on the Move / remove tab instead."/>
            </StackPanel>
          </Border>
        </Grid>
      </TabItem>

      <!-- ===== TAB 4 : LOAD CLASSIFICATION CHECK ===== -->
      <TabItem Header="&#x2714;  Load classification check">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="340"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <WrapPanel DockPanel.Dock="Top">
                <TextBlock Text="Panel:" VerticalAlignment="Center" Margin="0,0,4,0" FontWeight="SemiBold"/>
                <ComboBox x:Name="cmbLcPanel"/>
                <Button x:Name="btnLcRun" Style="{StaticResource BtnGreen}" Content="&#x1F50D; Run check"/>
              </WrapPanel>
              <WrapPanel DockPanel.Dock="Top" Margin="0,2,0,2">
                <CheckBox x:Name="chkLcDown" IsChecked="True" Margin="4"
                          Content="Include downstream panels / transformers"/>
                <CheckBox x:Name="chkLcDeep" IsChecked="True" Margin="4"
                          Content="Read the families' connectors (slower, finds hidden causes)"/>
              </WrapPanel>
              <Border DockPanel.Dock="Top" x:Name="bdLcVerdict" CornerRadius="8" Padding="10,6"
                      Margin="0,4,0,6" Background="#EEF2F8" BorderBrush="#C9D4E3" BorderThickness="1">
                <ScrollViewer MaxHeight="130" VerticalScrollBarVisibility="Auto">
                  <TextBlock x:Name="txtLcVerdict" TextWrapping="Wrap" Foreground="#1B2A41"
                             Text="Choose a panel and press Run check. Every load on the panel is listed under the load classification its electrical connector really uses."/>
                </ScrollViewer>
              </Border>
              <ListBox x:Name="lstLcRows" SelectionMode="Extended"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <DockPanel>
              <StackPanel DockPanel.Dock="Bottom">
                <TextBlock Style="{StaticResource H2}" Margin="0,10,0,4" Text="Fix"/>
                <TextBlock Style="{StaticResource Hint}"
                           Text="Set the selected rows (a group header = all its rows) to:"/>
                <ComboBox x:Name="cmbLcTarget" MinWidth="0"/>
                <Button x:Name="btnLcFixSel" Style="{StaticResource BtnGreen}" Content="&#x2714; Apply to selected rows"/>
                <Button x:Name="btnLcFixAll" Style="{StaticResource Btn3D}" Content="Apply to ALL loads on the panel"/>
                <Button x:Name="btnLcSelect" Style="{StaticResource Btn3D}" Content="Select rows in Revit"/>
              </StackPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}" Text="Load classifications in project"/>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource Hint}"
                         Text="Red = another item has the same name (ignoring case and spaces). A dot marks a hidden space at the start or end of a name."/>
              <ListBox x:Name="lstLcProject"/>
            </DockPanel>
          </Border>
        </Grid>
      </TabItem>
    </TabControl>

    <!-- ===== Log ===== -->
    <Border Grid.Row="2" Style="{StaticResource Card}" Padding="8">
      <DockPanel>
        <Button DockPanel.Dock="Right" x:Name="btnClose" Style="{StaticResource Btn3D}"
                Content="Close" VerticalAlignment="Bottom" MinWidth="100"/>
        <TextBox x:Name="txtLog" IsReadOnly="True" BorderThickness="0" Background="Transparent"
                 FontFamily="Consolas" FontSize="12" Foreground="#24364F"
                 VerticalScrollBarVisibility="Auto" TextWrapping="Wrap"/>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


# =============================================================================
# State kept between dialog openings (the dialog closes while you pick)
# =============================================================================
STATE = {
    "fixtures": [],           # element id ints for the Create tab
    "tab": 0,
    "log": [],
    "create_panel": None,     # panel id int or None
    "skip": True,
    "filter_panel": "__all__",
    "search": u"",
    "preselect": set(),       # circuit ids to select on the Manage tab
    "move_panel": None,
    "edit_circuit": None,
    "lc_panel": None,
    "lc_down": True,
    "lc_deep": True,
    "pending": None,          # "pick_fixtures" | "pick_circuits" | "add_elems"
}


def log(msg):
    STATE["log"].append(msg)


class CircuitManagerWindow(object):

    def __init__(self):
        self.win = XamlReader.Parse(XAML)
        f = self.win.FindName
        self.c = dict((n, f(n)) for n in [
            "tabs", "txtDoc", "lblFixtures", "lstFixtures", "btnPickFix", "btnRemFix",
            "btnClearFix", "cmbCreatePanel", "chkSkip", "btnCreate",
            "lblCircuits", "cmbFilterPanel", "txtSearch", "btnSelAll", "btnSelNone",
            "btnPickCkt", "btnRefresh", "lstCircuits", "lblSelCount", "cmbMovePanel",
            "btnMove", "btnDisconnect", "btnEditSel", "btnDelete",
            "cmbEditCircuit", "lblEditElems", "lstEditElems", "btnAddElems",
            "btnRemElems", "txtLog", "btnClose",
            "cmbLcPanel", "btnLcRun", "chkLcDown", "chkLcDeep", "bdLcVerdict",
            "txtLcVerdict", "lstLcRows", "cmbLcTarget", "btnLcFixSel", "btnLcFixAll",
            "btnLcSelect", "lstLcProject"])
        self.lc = None
        self.lc_group_ids = {}
        self.busy = False
        self.c["txtDoc"].Text = u"Project: {0}   -   each action is one Undo step".format(doc.Title)

        self.fill_panel_combos()
        self.c["chkSkip"].IsChecked = STATE["skip"]
        self.fill_fixtures()
        self.fill_circuits()
        self.fill_edit_combo()
        self.c["chkLcDown"].IsChecked = STATE["lc_down"]
        self.c["chkLcDeep"].IsChecked = STATE["lc_deep"]
        _lcs = all_load_classifications()
        self.fill_lc_project(_lcs)
        self.fill_lc_target(_lcs)
        self.refresh_log()
        self.c["tabs"].SelectedIndex = STATE["tab"]

        ev = [
            ("btnPickFix", self.on_pick_fixtures), ("btnRemFix", self.on_remove_fix),
            ("btnClearFix", self.on_clear_fix), ("btnCreate", self.on_create),
            ("btnSelAll", self.on_sel_all), ("btnSelNone", self.on_sel_none),
            ("btnPickCkt", self.on_pick_circuits), ("btnRefresh", self.on_refresh),
            ("btnMove", self.on_move), ("btnDisconnect", self.on_disconnect),
            ("btnEditSel", self.on_edit_selected), ("btnDelete", self.on_delete),
            ("btnAddElems", self.on_add_elems), ("btnRemElems", self.on_remove_elems),
            ("btnClose", self.on_close),
            ("btnLcRun", self.on_lc_run), ("btnLcFixSel", self.on_lc_fix_sel),
            ("btnLcFixAll", self.on_lc_fix_all), ("btnLcSelect", self.on_lc_select),
        ]
        for name, handler in ev:
            self.c[name].Click += self.safe(handler)
        self.c["cmbFilterPanel"].SelectionChanged += self.safe(self.on_filter_changed)
        self.c["txtSearch"].TextChanged += self.safe(self.on_filter_changed)
        self.c["lstCircuits"].SelectionChanged += self.safe(self.on_circuit_sel_changed)
        self.c["cmbEditCircuit"].SelectionChanged += self.safe(self.on_edit_circuit_changed)

    def safe(self, handler):
        """Never let an error escape a WPF event (it could crash Revit)."""
        def run(sender, args):
            if self.busy:
                return
            self.busy = True
            try:
                handler(sender, args)
            except Exception as ex:
                log(u"Error: {0}".format(ex))
                try:
                    self.refresh_log()
                except Exception:
                    pass
            finally:
                self.busy = False
        return run

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def combo_item(text, tag):
        it = ComboBoxItem()
        it.Content = text
        it.Tag = tag
        return it

    @staticmethod
    def list_item(text, tag):
        it = ListBoxItem()
        it.Content = text
        it.Tag = tag
        return it

    @staticmethod
    def select_combo_tag(combo, tag):
        for i in range(combo.Items.Count):
            if combo.Items[i].Tag == tag:
                combo.SelectedIndex = i
                return
        if combo.Items.Count:
            combo.SelectedIndex = 0

    @staticmethod
    def combo_tag(combo):
        it = combo.SelectedItem
        return it.Tag if it is not None else None

    @staticmethod
    def selected_tags(listbox):
        return [it.Tag for it in listbox.SelectedItems]

    def refresh_log(self):
        self.c["txtLog"].Text = u"\n".join(STATE["log"][-200:]) or \
            u"Ready. Pick fixtures on the first tab, or manage existing circuits on the others."
        self.c["txtLog"].ScrollToEnd()

    def save_state(self):
        STATE["tab"] = self.c["tabs"].SelectedIndex
        STATE["create_panel"] = self.combo_tag(self.c["cmbCreatePanel"])
        STATE["skip"] = bool(self.c["chkSkip"].IsChecked)
        STATE["filter_panel"] = self.combo_tag(self.c["cmbFilterPanel"])
        STATE["search"] = self.c["txtSearch"].Text or u""
        STATE["move_panel"] = self.combo_tag(self.c["cmbMovePanel"])
        STATE["edit_circuit"] = self.combo_tag(self.c["cmbEditCircuit"])
        STATE["preselect"] = set(self.selected_tags(self.c["lstCircuits"]))
        STATE["lc_panel"] = self.combo_tag(self.c["cmbLcPanel"])
        STATE["lc_down"] = bool(self.c["chkLcDown"].IsChecked)
        STATE["lc_deep"] = bool(self.c["chkLcDeep"].IsChecked)

    def close_for(self, pending):
        self.save_state()
        STATE["pending"] = pending
        self.win.Close()

    # ------------------------------------------------------------- fill lists
    def fill_panel_combos(self):
        self.panels = all_panels()
        cp, fp, mp = self.c["cmbCreatePanel"], self.c["cmbFilterPanel"], self.c["cmbMovePanel"]
        lp = self.c["cmbLcPanel"]
        cp.Items.Clear(); fp.Items.Clear(); mp.Items.Clear(); lp.Items.Clear()
        cp.Items.Add(self.combo_item(u"(no panel - leave unassigned)", None))
        fp.Items.Add(self.combo_item(u"(all circuits)", "__all__"))
        fp.Items.Add(self.combo_item(u"(circuits with no panel)", "__none__"))
        for p in self.panels:
            pid = eid_val(p.Id)
            name = panel_name(p)
            cp.Items.Add(self.combo_item(name, pid))
            fp.Items.Add(self.combo_item(name, pid))
            mp.Items.Add(self.combo_item(name, pid))
            lp.Items.Add(self.combo_item(name, pid))
        self.select_combo_tag(cp, STATE["create_panel"])
        self.select_combo_tag(fp, STATE["filter_panel"])
        self.select_combo_tag(mp, STATE["move_panel"])
        self.select_combo_tag(lp, STATE["lc_panel"])
        self.c["txtSearch"].Text = STATE["search"]

    def fill_fixtures(self):
        lst = self.c["lstFixtures"]
        lst.Items.Clear()
        alive = []
        for v in STATE["fixtures"]:
            el = get_el(v)
            if el is None:
                continue
            alive.append(v)
            txt = element_label(el)
            ps = power_systems(el)
            if ps:
                txt += u"   * on circuit {0}".format(
                    u", ".join((s.PanelName or u"?") + u"/" + (s.CircuitNumber or u"-") for s in ps))
            lst.Items.Add(self.list_item(txt, v))
        STATE["fixtures"] = alive
        self.c["lblFixtures"].Text = u"Fixtures to circuit ({0})".format(len(alive))

    def fill_circuits(self, keep_selection=None):
        lst = self.c["lstCircuits"]
        if keep_selection is None:
            keep_selection = set(self.selected_tags(lst)) | STATE["preselect"]
        STATE["preselect"] = set()
        filt = self.combo_tag(self.c["cmbFilterPanel"])
        search = (self.c["txtSearch"].Text or u"").lower().strip()
        lst.Items.Clear()
        self.circuits = all_circuits()
        shown = 0
        for c in self.circuits:
            if filt == "__none__" and c.BaseEquipment is not None:
                continue
            if filt not in (None, "__all__", "__none__"):
                if c.BaseEquipment is None or eid_val(c.BaseEquipment.Id) != filt:
                    continue
            label = circuit_label(c)
            if search and search not in label.lower():
                continue
            cid = eid_val(c.Id)
            it = self.list_item(label, cid)
            lst.Items.Add(it)
            if cid in keep_selection:
                it.IsSelected = True
            shown += 1
        self.c["lblCircuits"].Text = u"Circuits  ({0} shown of {1})".format(shown, len(self.circuits))
        self.update_sel_count()

    def fill_edit_combo(self):
        cmb = self.c["cmbEditCircuit"]
        cmb.Items.Clear()
        for c in all_circuits():
            cmb.Items.Add(self.combo_item(circuit_label(c), eid_val(c.Id)))
        self.select_combo_tag(cmb, STATE["edit_circuit"])
        self.fill_edit_elems()

    def fill_edit_elems(self):
        lst = self.c["lstEditElems"]
        lst.Items.Clear()
        cid = self.combo_tag(self.c["cmbEditCircuit"])
        c = get_el(cid) if cid is not None else None
        if c is None:
            self.c["lblEditElems"].Text = u"No circuit selected"
            return
        n = 0
        for e in c.Elements:
            lst.Items.Add(self.list_item(element_label(e), eid_val(e.Id)))
            n += 1
        self.c["lblEditElems"].Text = u"{0} element(s) on this circuit".format(n)

    def refresh_all(self):
        keep = set(self.selected_tags(self.c["lstCircuits"]))
        STATE["edit_circuit"] = self.combo_tag(self.c["cmbEditCircuit"])
        self.fill_fixtures()
        self.fill_circuits(keep)
        self.fill_edit_combo()
        self.refresh_log()

    def update_sel_count(self):
        n = self.c["lstCircuits"].SelectedItems.Count
        self.c["lblSelCount"].Text = u"{0} circuit(s) selected".format(n)

    def selected_circuits(self):
        out = []
        for v in self.selected_tags(self.c["lstCircuits"]):
            c = get_el(v)
            if c is not None:
                out.append(c)
        return out

    # ------------------------------------------------------------ TAB 1 events
    def on_pick_fixtures(self, s, e):
        self.close_for("pick_fixtures")

    def on_remove_fix(self, s, e):
        rem = set(self.selected_tags(self.c["lstFixtures"]))
        STATE["fixtures"] = [v for v in STATE["fixtures"] if v not in rem]
        self.fill_fixtures()

    def on_clear_fix(self, s, e):
        STATE["fixtures"] = []
        self.fill_fixtures()

    def on_create(self, s, e):
        if not STATE["fixtures"]:
            log(u"Create: the list is empty - pick fixtures first.")
            self.refresh_log()
            return
        skip = bool(self.c["chkSkip"].IsChecked)
        pid = self.combo_tag(self.c["cmbCreatePanel"])
        panel = get_el(pid) if pid is not None else None
        made, skipped, failed, no_panel = 0, 0, 0, 0
        with Tx("Create circuits"):
            for v in STATE["fixtures"]:
                el = get_el(v)
                if el is None:
                    continue
                if skip and power_systems(el):
                    skipped += 1
                    continue
                ids = List[DB.ElementId]()
                ids.Add(el.Id)
                try:
                    sys_ = ElectricalSystem.Create(doc, ids, ElectricalSystemType.PowerCircuit)
                except Exception as ex:
                    failed += 1
                    log(u"  x {0}: {1}".format(element_label(el), ex))
                    continue
                if sys_ is None:
                    failed += 1
                    log(u"  x {0}: Revit returned no circuit".format(element_label(el)))
                    continue
                made += 1
                if panel is not None:
                    try:
                        sys_.SelectPanel(panel)
                    except Exception as ex:
                        no_panel += 1
                        log(u"  ! circuit for id {0} not assigned to {1}: {2}".format(
                            v, panel_name(panel), ex))
        msg = u"Create: {0} circuit(s) created".format(made)
        if panel is not None:
            msg += u" on {0}".format(panel_name(panel))
        if skipped:
            msg += u", {0} skipped (already circuited)".format(skipped)
        if failed:
            msg += u", {0} failed".format(failed)
        if no_panel:
            msg += u", {0} left without panel".format(no_panel)
        log(msg + u".")
        self.refresh_all()

    # ------------------------------------------------------------ TAB 2 events
    def on_filter_changed(self, s, e):
        if self.c.get("lstCircuits") is not None and hasattr(self, "circuits"):
            self.fill_circuits()

    def on_circuit_sel_changed(self, s, e):
        self.update_sel_count()

    def on_sel_all(self, s, e):
        self.c["lstCircuits"].SelectAll()
        self.update_sel_count()

    def on_sel_none(self, s, e):
        self.c["lstCircuits"].UnselectAll()
        self.update_sel_count()

    def on_pick_circuits(self, s, e):
        self.close_for("pick_circuits")

    def on_refresh(self, s, e):
        self.fill_panel_combos()
        self.refresh_all()

    def on_move(self, s, e):
        circuits = self.selected_circuits()
        pid = self.combo_tag(self.c["cmbMovePanel"])
        panel = get_el(pid) if pid is not None else None
        if not circuits or panel is None:
            log(u"Move: select circuits and a target panel first.")
            self.refresh_log()
            return
        ok, bad = 0, 0
        with Tx("Move circuits"):
            for c in circuits:
                label = circuit_label(c)
                try:
                    c.SelectPanel(panel)
                    ok += 1
                except Exception as ex:
                    bad += 1
                    log(u"  x {0}: {1}".format(label, ex))
        log(u"Move: {0} circuit(s) moved to {1}{2}.".format(
            ok, panel_name(panel), u", {0} failed".format(bad) if bad else u""))
        self.refresh_all()

    def on_disconnect(self, s, e):
        circuits = self.selected_circuits()
        if not circuits:
            log(u"Disconnect: select circuits first.")
            self.refresh_log()
            return
        ok, bad = 0, 0
        with Tx("Disconnect circuits"):
            for c in circuits:
                if c.BaseEquipment is None:
                    continue
                try:
                    c.DisconnectPanel()
                    ok += 1
                except Exception as ex:
                    bad += 1
                    log(u"  x {0}: {1}".format(circuit_label(c), ex))
        log(u"Disconnect: {0} circuit(s) disconnected from their panel{1}.".format(
            ok, u", {0} failed".format(bad) if bad else u""))
        self.refresh_all()

    def on_edit_selected(self, s, e):
        tags = self.selected_tags(self.c["lstCircuits"])
        if not tags:
            return
        self.select_combo_tag(self.c["cmbEditCircuit"], tags[0])
        self.fill_edit_elems()
        self.c["tabs"].SelectedIndex = 2

    def on_delete(self, s, e):
        circuits = self.selected_circuits()
        if not circuits:
            log(u"Delete: select circuits first.")
            self.refresh_log()
            return
        from System.Windows import MessageBox, MessageBoxButton, MessageBoxResult
        ans = MessageBox.Show(
            u"Delete {0} circuit(s)?\n\nThe fixtures stay in the model, only the "
            u"circuits are removed. You can Undo this in Revit.".format(len(circuits)),
            u"Circuit Manager Pro", MessageBoxButton.YesNo)
        if ans != MessageBoxResult.Yes:
            return
        ok, bad = 0, 0
        with Tx("Delete circuits"):
            for c in circuits:
                label = circuit_label(c)
                try:
                    doc.Delete(c.Id)
                    ok += 1
                except Exception as ex:
                    bad += 1
                    log(u"  x {0}: {1}".format(label, ex))
        log(u"Delete: {0} circuit(s) deleted{1}.".format(
            ok, u", {0} failed".format(bad) if bad else u""))
        self.refresh_all()

    # ------------------------------------------------------------ TAB 3 events
    def on_edit_circuit_changed(self, s, e):
        self.fill_edit_elems()

    def on_add_elems(self, s, e):
        if self.combo_tag(self.c["cmbEditCircuit"]) is None:
            log(u"Add: choose a circuit first.")
            self.refresh_log()
            return
        self.close_for("add_elems")

    def on_remove_elems(self, s, e):
        cid = self.combo_tag(self.c["cmbEditCircuit"])
        c = get_el(cid) if cid is not None else None
        elems = [get_el(v) for v in self.selected_tags(self.c["lstEditElems"])]
        elems = [x for x in elems if x is not None]
        if c is None or not elems:
            log(u"Remove: choose a circuit and select elements in the list.")
            self.refresh_log()
            return
        try:
            with Tx("Remove elements from circuit"):
                c.RemoveFromCircuit(make_set(elems))
            log(u"Remove: {0} element(s) removed from {1}.".format(len(elems), circuit_label(c)))
        except Exception as ex:
            log(u"Remove failed: {0}".format(ex))
        self.refresh_all()

    # ------------------------------------------------------- TAB 4 (load class)
    def fill_lc_project(self, lcs, usage=None):
        lst = self.c["lstLcProject"]
        lst.Items.Clear()
        dups = duplicate_groups(lcs)
        for v, lc in sorted(lcs.items(), key=lambda kv: (norm_name(kv[1].Name), kv[0])):
            txt = u"{0}   id {1}".format(lc_visible(lc.Name), v)
            if usage is not None:
                txt += u"   - {0} on panel".format(usage.get(v, 0))
            if v in dups:
                txt += u"\n      same name as id {0}".format(u", ".join(str(x) for x in dups[v]))
            it = self.list_item(txt, v)
            if v in dups:
                it.Foreground = Brushes.Firebrick
                it.FontWeight = FontWeights.Bold
            lst.Items.Add(it)

    def fill_lc_target(self, lcs, prefer=None):
        cmb = self.c["cmbLcTarget"]
        cmb.Items.Clear()
        for v, lc in sorted(lcs.items(), key=lambda kv: (norm_name(kv[1].Name), kv[0])):
            cmb.Items.Add(self.combo_item(u"{0}   (id {1})".format(lc_visible(lc.Name), v), v))
        self.select_combo_tag(cmb, prefer)

    def show_lc(self, res):
        self.lc = res
        self.lc_group_ids = {}
        lst = self.c["lstLcRows"]
        lst.Items.Clear()
        for gi, k in enumerate(res["order"]):
            rows = res["groups"][k]
            gtag = u"g:{0}".format(gi)
            self.lc_group_ids[gtag] = [r["id"] for r in rows if r["kind"] != "feeder"]
            head = self.list_item(u"\u25A0  {0}    [{1}]".format(res["labels"][k], len(rows)), gtag)
            head.FontWeight = FontWeights.Bold
            head.Background = BRUSH.ConvertFromString(
                "#FDE7E5" if (k is None or not isinstance(k, _ID_TYPES) or k in res["dups"])
                and k != "__info__" else "#E6EEF9")
            lst.Items.Add(head)
            for r in rows:
                txt = u"      {0}    {1}\n          via {2}".format(
                    r["ckt"], r["label"], u";  ".join(r["how"]) or u"-")
                for prob in r["problems"]:
                    txt += u"\n          \u26A0 " + prob
                it = self.list_item(txt, r["id"])
                if r["problems"]:
                    it.Foreground = Brushes.Firebrick
                lst.Items.Add(it)
        self.c["txtLcVerdict"].Text = u"\n".join(res["verdict"])
        color = {True: "#E3F6EA", False: "#FDE7E5"}.get(res["ok"], "#EEF2F8")
        self.c["bdLcVerdict"].Background = BRUSH.ConvertFromString(color)
        self.fill_lc_project(res["lcs"], res["usage"])
        prefer = None
        if res["usage"]:
            prefer = sorted(res["usage"].items(), key=lambda kv: -kv[1])[0][0]
        self.fill_lc_target(res["lcs"], prefer)

    def on_lc_run(self, s, e):
        pid = self.combo_tag(self.c["cmbLcPanel"])
        panel = get_el(pid) if pid is not None else None
        if panel is None:
            self.c["txtLcVerdict"].Text = u"Choose a panel first."
            return
        res = run_lc_check(panel, bool(self.c["chkLcDown"].IsChecked),
                           bool(self.c["chkLcDeep"].IsChecked))
        self.show_lc(res)
        log(u"Load classification check of '{0}':".format(res["panel"]))
        for line in res["verdict"]:
            log(u"   " + line)
        self.refresh_log()

    def lc_selected_ids(self):
        ids = []
        for t in self.selected_tags(self.c["lstLcRows"]):
            if hasattr(t, "startswith"):
                more = self.lc_group_ids.get(t, [])
            else:
                more = [t]
            for v in more:
                if v not in ids:
                    ids.append(v)
        return ids

    def lc_apply(self, ids):
        if not getattr(self, "lc", None):
            log(u"Fix: run the check first.")
            self.refresh_log()
            return
        tgt = self.combo_tag(self.c["cmbLcTarget"])
        rows = dict((r["id"], r) for r in self.lc["rows"] if r["kind"] != "feeder")
        ids = [v for v in ids if v in rows]
        if tgt is None or not ids:
            log(u"Fix: select rows and a load classification first.")
            self.refresh_log()
            return
        lcs = all_load_classifications()
        tgt_id = to_eid(tgt)
        done_types, changed, in_family, failed, types = set(), 0, 0, 0, 0
        with Tx("Unify load classification"):
            for v in ids:
                el = get_el(v)
                if el is None:
                    continue
                params = lc_params(el, lcs)
                drive = rows[v].get("drive")
                if drive is not None:
                    params = [(w, p) for w, p in params if p.Definition.Name in drive]
                if not params:
                    in_family += 1
                    log(u"  ! {0}: the load classification is fixed inside the family - "
                        u"edit the family's electrical connector.".format(rows[v]["label"]))
                    continue
                for where, p in params:
                    if where == "type":
                        tkey = eid_val(el.Symbol.Id)
                        if tkey in done_types:
                            continue
                        done_types.add(tkey)
                    if p.IsReadOnly:
                        failed += 1
                        log(u"  x {0}: '{1}' is read-only".format(rows[v]["label"], p.Definition.Name))
                        continue
                    if eid_val(p.AsElementId()) == tgt:
                        continue
                    try:
                        p.Set(tgt_id)
                        changed += 1
                        if where == "type":
                            types += 1
                    except Exception as ex:
                        failed += 1
                        log(u"  x {0}: {1}".format(rows[v]["label"], ex))
        msg = u"Fix: {0} parameter(s) set to {1}".format(changed, lc_visible(lcs[tgt].Name) if tgt in lcs else tgt)
        if types:
            msg += u" ({0} on types - every instance of those types changes, also on other panels)".format(types)
        if in_family:
            msg += u", {0} fixed inside the family".format(in_family)
        if failed:
            msg += u", {0} failed".format(failed)
        log(msg + u".")
        self.on_lc_run(None, None)
        self.refresh_all()

    def on_lc_fix_sel(self, s, e):
        self.lc_apply(self.lc_selected_ids())

    def on_lc_fix_all(self, s, e):
        if getattr(self, "lc", None):
            self.lc_apply([r["id"] for r in self.lc["rows"] if r["kind"] != "feeder"])

    def on_lc_select(self, s, e):
        ids = List[DB.ElementId]()
        for v in self.lc_selected_ids():
            ids.Add(to_eid(v))
        uidoc.Selection.SetElementIds(ids)
        log(u"Select: {0} element(s) selected in Revit (visible after closing the dialog).".format(ids.Count))
        self.refresh_log()

    def on_close(self, s, e):
        self.save_state()
        STATE["pending"] = None
        self.win.Close()

    def show(self):
        self.win.ShowDialog()


# =============================================================================
# Pick actions (run while the dialog is closed, then it reopens)
# =============================================================================
def do_pick_fixtures():
    picked = pick_elements("Pick fixtures / devices to circuit, then click Finish")
    have = set(STATE["fixtures"])
    added, ignored = 0, 0
    for el in picked:
        if not is_circuitable(el):
            ignored += 1
            continue
        v = eid_val(el.Id)
        if v not in have:
            STATE["fixtures"].append(v)
            have.add(v)
            added += 1
    log(u"Pick: {0} element(s) added to the list{1}.".format(
        added, u", {0} ignored (no electrical connector)".format(ignored) if ignored else u""))
    STATE["tab"] = 0


def do_pick_circuits():
    picked = pick_elements("Pick elements - their circuits get selected - then click Finish")
    ids = set()
    for el in picked:
        if isinstance(el, ElectricalSystem):
            ids.add(eid_val(el.Id))
            continue
        for s in element_systems(el):
            ids.add(eid_val(s.Id))
    STATE["preselect"] = ids
    STATE["filter_panel"] = "__all__"
    STATE["search"] = u""
    log(u"Select: {0} circuit(s) found on the picked elements.".format(len(ids)))
    STATE["tab"] = 1


def do_add_elems():
    c = get_el(STATE["edit_circuit"]) if STATE["edit_circuit"] is not None else None
    STATE["tab"] = 2
    if c is None:
        return
    picked = [el for el in pick_elements("Pick elements to ADD to the circuit, then click Finish")
              if is_circuitable(el)]
    if not picked:
        log(u"Add: nothing picked.")
        return
    try:
        with Tx("Add elements to circuit"):
            c.AddToCircuit(make_set(picked))
        log(u"Add: {0} element(s) added to {1}.".format(len(picked), circuit_label(c)))
    except Exception as ex:
        log(u"Add failed: {0}".format(ex))


# =============================================================================
# Main
# =============================================================================
run = True
try:
    run = bool(IN[0]) if len(IN) > 0 and IN[0] is not None else True
except Exception:
    pass

if not run:
    OUT = "Set IN[0] to True and run the graph to open Circuit Manager Pro."
else:
    # Dynamo keeps its own transaction open; close it so every action below is
    # a separate, undoable Revit transaction.
    TransactionManager.Instance.ForceCloseTransaction()
    actions = {
        "pick_fixtures": do_pick_fixtures,
        "pick_circuits": do_pick_circuits,
        "add_elems": do_add_elems,
    }
    while True:
        STATE["pending"] = None
        try:
            CircuitManagerWindow().show()
        except Exception as ex:
            log(u"Dialog error: {0}".format(ex))
            break
        action = actions.get(STATE["pending"])
        if action is None:
            break
        action()
    OUT = STATE["log"] if STATE["log"] else ["No changes made."]
