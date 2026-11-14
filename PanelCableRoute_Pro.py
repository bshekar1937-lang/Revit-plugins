# -*- coding: utf-8 -*-
__title__ = "Panel Cable Route Pro"
__doc__ = (
    "Measure the cable tray route between every electrical panel, and from "
    "each panel to the equipment and isolators on its circuits. Lengths come "
    "from the modelled tray centre lines, so they can be used for "
    "voltage-drop calculations. A tabbed preview shows the routes in 3D "
    "before anything is drawn; routes can then be drawn as red detail lines "
    "and exported to Excel."
)

# =============================================================================
# PANEL CABLE ROUTE PRO  -  panel to panel lengths along the modelled trays
# -----------------------------------------------------------------------------
# What it does
#   1. A dialog collects the scope (which panels, panel-to-panel pairs,
#      circuit equipment, isolators), the cable allowance and the
#      voltage-drop figures.
#   2. Every modelled cable tray centre line becomes an edge of one network.
#      Tray fittings bridge the tray ends they connect, and ends that merely
#      meet within the connection tolerance are joined as well.
#   3. Each panel and each piece of equipment is attached to the network at
#      the nearest tray, orthogonally (across, along, then up), preferring a
#      vertical riser when the equipment sits on another level.
#   4. ONE Dijkstra pass per panel gives that panel's distance to every other
#      attachment point, so a whole building of panels is measured in P
#      passes instead of P x P.
#   5. A tabbed preview is shown BEFORE anything is created:
#        - 3D preview : the tray network, the panels and every route, drawn
#          in a WPF 3D view you can orbit. Click a route to highlight it.
#        - Routes     : one row per connection - length, current, voltage
#          drop and whether it is within the limit. Filter, sort, select the
#          elements in Revit, and tick the routes to draw or export.
#        - Panels     : one row per panel - how many connections, the longest
#          route and the worst voltage drop.
#        - Issues     : equipment that could not be reached, and routes that
#          had to bridge a gap in the modelled trays.
#   6. From there: draw the red detail lines, export to Excel or CSV, or
#      close without changing anything.
#
# Voltage drop
#   drop (V) = mV/A/m x current (A) x route length (m) / 1000
#   The current and the voltage come from the Revit circuit when the
#   connection is a circuit; otherwise the dialog's default current is used.
#   mV/A/m is the figure for the cable size you intend to use - it is a
#   property of the cable, not of the model, so it is entered in the dialog.
#
# MODIFIES THE MODEL only when you ask it to draw: it creates view-specific
# red detail lines (and the "Auto Cable Route - Red" line subcategory) in one
# transaction, and deletes the ones a previous run drew in the same views.
# A single Undo (Ctrl+Z) removes everything a run created.
#
# Revit versions: 2023, 2024, 2025, 2026, 2027 (pyRevit, IronPython engine).
#   - Element ids are read through element_id_value(): ElementId.IntegerValue
#     was removed in Revit 2026 (ElementId.Value exists from 2024).
#   - Currents and voltages are converted with UnitTypeId (Revit 2021+).
# =============================================================================

import math
import heapq

from Autodesk.Revit import DB
from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, Color, CurveElement, ElementId,
    FilteredElementCollector, GraphicsStyleType, Line, LocationCurve,
    LocationPoint, Transaction, ViewPlan, XYZ
)
from Autodesk.Revit.DB.Electrical import ElectricalSystem
from pyrevit import forms, revit, script

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System.Collections.Generic import List
from System.Windows import (
    Clipboard, GridLength, GridUnitType, HorizontalAlignment,
    SystemParameters, TextWrapping, Thickness, VerticalAlignment
)
from System.Windows.Controls import (
    Border, Button, CheckBox, ColumnDefinition, Grid, Orientation,
    StackPanel, TextBlock
)
from System.Windows.Media import Color as MediaColor, SolidColorBrush
from System.Windows.Media.Media3D import (
    AmbientLight, DiffuseMaterial, DirectionalLight, GeometryModel3D,
    MeshGeometry3D, Model3DGroup, ModelVisual3D, PerspectiveCamera, Point3D,
    ScaleTransform3D, Transform3DGroup, TranslateTransform3D, Vector3D
)


doc = revit.doc
uidoc = revit.uidoc
view = doc.ActiveView
output = script.get_output()

TITLE = "Panel Cable Route Pro"
FT_TO_M = 0.3048
MM_TO_FT = 1.0 / 304.8
EPSILON = 1e-9

PLAN_VIEW_TYPES = ("FloorPlan", "CeilingPlan", "EngineeringPlan")
ROUTE_STYLE_NAME = "Auto Cable Route - Red"

# How far apart two tray ends may be and still count as connected.
DEFAULT_CONNECTION_TOLERANCE_MM = 100.0
# Attachment candidates kept per equipment item (the nearest tray points).
MAX_ATTACH_CANDIDATES = 8
# A cable leaves the tray at the point nearest the equipment. Candidates whose
# orthogonal gap is more than this beyond the nearest one are dropped: without
# that, projecting onto every tray in the building invents an attachment at
# every node whose cost merely repeats the travel along the trays, which both
# hides the real tray route and mis-states the connection lengths.
ATTACH_GAP_SLACK_MM = 2000.0
# Two routes whose totals are within this are the same length as far as
# anyone cares, so a tie-break can then choose on something more useful.
TIE_TOLERANCE_MM = 1.0

DEFAULT_SETTINGS = {
    "scope_pairs": "1",            # panel to panel, every pair
    "scope_circuits": "1",         # panel to the equipment on its circuits
    "scope_isolators": "1",        # include isolators as destinations
    "allowance": "5",              # cable allowance %
    "tolerance": "100",            # tray connection tolerance, mm
    "voltage": "400",              # nominal voltage when the circuit has none
    "current": "32",               # load current when the circuit has none
    "mv_per_a_m": "1.1",           # cable mV/A/m for the drop calculation
    "limit_percent": "5",          # voltage drop limit %
    "draw_lines": "1",             # draw the red detail lines
    "max_pair_metres": "0",        # 0 = no limit on a pair's route length
}


def load_settings():
    """Remembered dialog values (per pyRevit user config)."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        config = script.get_config()
    except Exception:
        return settings, None
    for key in DEFAULT_SETTINGS:
        try:
            stored = config.get_option(key, None)
        except Exception:
            stored = None
        if stored is not None and str(stored).strip() != "":
            settings[key] = str(stored)
    return settings, config


def save_settings(config, settings):
    if config is None or not settings:
        return
    try:
        for key, value in settings.items():
            setattr(config, key, value)
        script.save_config()
    except Exception:
        pass        # remembering settings is only a convenience


# =============================================================================
# Units and ids
# =============================================================================
def metres(feet):
    return feet * FT_TO_M


def feet(millimetres):
    return millimetres * MM_TO_FT


def element_id_value(element_id):
    """Integer behind an ElementId, on every supported Revit version.

    ElementId.IntegerValue was removed in Revit 2026; ElementId.Value is
    there from 2024."""
    if element_id is None:
        return None
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def category_id_value(element):
    try:
        if element.Category is None:
            return None
        return element_id_value(element.Category.Id)
    except Exception:
        return None


def is_category(element, builtin_category):
    try:
        return category_id_value(element) == int(builtin_category)
    except Exception:
        return False


def converted(value, unit_name, fallback=None):
    """Internal units -> the named display unit, if this Revit knows it."""
    try:
        unit = getattr(DB.UnitTypeId, unit_name)
    except AttributeError:
        return value if fallback is None else fallback
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(value, unit)
    except Exception:
        return value if fallback is None else fallback


def current_amperes(value):
    return converted(value, "Amperes")


def voltage_volts(value):
    return converted(value, "Volts")

# =============================================================================
# Reading the elements (names, comments, isolators, points)
# =============================================================================
NAME_PARAMETERS = (
    "Panel Name", "Panel", "Mark", "Equipment Name", "Type Mark"
)


def _builtin_parameter(element, parameter_id):
    try:
        return element.get_Parameter(parameter_id)
    except Exception:
        return None


def _named_parameter(element, parameter_name):
    try:
        return element.LookupParameter(parameter_name)
    except Exception:
        return None


def _parameter_text(parameter):
    if parameter is None:
        return ""
    try:
        return parameter.AsString() or parameter.AsValueString() or ""
    except Exception:
        return ""


def get_name(element):
    """Short name: the first of the name parameters that is filled in."""
    for parameter_name in NAME_PARAMETERS:
        value = _parameter_text(_named_parameter(element, parameter_name))
        if value:
            return value
    try:
        if element.Name:
            return element.Name
    except Exception:
        pass
    return "Element {}".format(element_id_value(element.Id))


def get_comments(element):
    for parameter in (
            _builtin_parameter(
                element, BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS),
            _named_parameter(element, "Comments")):
        value = _parameter_text(parameter)
        if value:
            return value
    return ""


def get_type_comments(element):
    try:
        symbol = doc.GetElement(element.GetTypeId())
    except Exception:
        symbol = None
    if symbol is None:
        return ""
    for parameter in (
            _builtin_parameter(
                symbol, BuiltInParameter.ALL_MODEL_TYPE_COMMENTS),
            _named_parameter(symbol, "Comments")):
        value = _parameter_text(parameter)
        if value:
            return value
    return ""


def get_isolator_rating(element):
    parameter = _named_parameter(element, "JAC-EL-Rating")
    if parameter is None:
        try:
            symbol = doc.GetElement(element.GetTypeId())
        except Exception:
            symbol = None
        if symbol is not None:
            parameter = _named_parameter(symbol, "JAC-EL-Rating")
    return _parameter_text(parameter)


def get_type_name_text(element):
    """Instance, type and family names in one lower-case string."""
    names = []
    try:
        if element.Name:
            names.append(element.Name)
    except Exception:
        pass
    try:
        symbol = doc.GetElement(element.GetTypeId())
        if symbol is not None:
            if symbol.Name:
                names.append(symbol.Name)
            family = getattr(symbol, "Family", None)
            if family is not None and family.Name:
                names.append(family.Name)
    except Exception:
        pass
    return " ".join(names).lower()


def is_isolator(element):
    """An isolator is tagged in Comments / JAC-EL-Rating, or is an electrical
    fixture whose family or type is named one."""
    text = "{} {}".format(
        get_comments(element), get_type_comments(element)
    ).lower()
    if "isolator" in text or "isolator" in get_isolator_rating(element).lower():
        return True
    if not is_category(element, BuiltInCategory.OST_ElectricalFixtures):
        return False
    return "isolator" in get_type_name_text(element)


def get_panel_assignment(element):
    return _parameter_text(_named_parameter(element, "Panel"))


def get_level_name(element):
    level_id = get_element_level_id(element)
    if level_id is None:
        return ""
    try:
        level = doc.GetElement(level_id)
        return level.Name if level is not None else ""
    except Exception:
        return ""


def get_element_level_id(element):
    try:
        level_id = element.LevelId
        if level_id is not None and level_id != ElementId.InvalidElementId:
            return level_id
    except Exception:
        pass
    parameter = _builtin_parameter(
        element, BuiltInParameter.FAMILY_LEVEL_PARAM)
    if parameter is not None:
        try:
            level_id = parameter.AsElementId()
            if level_id is not None and level_id != ElementId.InvalidElementId:
                return level_id
        except Exception:
            pass
    return None


def get_element_connectors(element):
    try:
        return list(element.MEPModel.ConnectorManager.Connectors)
    except Exception:
        pass
    try:
        return list(element.ConnectorManager.Connectors)
    except Exception:
        return []


def get_points(element):
    """Points the cable could leave from: connectors, else the insertion
    point, else the ends of its curve, else the centre of its box."""
    points = []
    for connector in get_element_connectors(element):
        try:
            points.append(connector.Origin)
        except Exception:
            pass
    if points:
        return points
    try:
        if isinstance(element.Location, LocationPoint):
            return [element.Location.Point]
    except Exception:
        pass
    try:
        if isinstance(element.Location, LocationCurve):
            curve = element.Location.Curve
            return [curve.GetEndPoint(0), curve.GetEndPoint(1)]
    except Exception:
        pass
    try:
        box = element.get_BoundingBox(None)
        if box is not None:
            return [XYZ((box.Min.X + box.Max.X) / 2.0,
                        (box.Min.Y + box.Max.Y) / 2.0,
                        (box.Min.Z + box.Max.Z) / 2.0)]
    except Exception:
        pass
    return []


def element_bounds(element):
    """(min, max) tuples of an element's box, for the 3D preview."""
    try:
        box = element.get_BoundingBox(None)
    except Exception:
        box = None
    if box is None:
        points = get_points(element)
        if not points:
            return None
        pad = feet(150.0)
        return (
            (min(point.X for point in points) - pad,
             min(point.Y for point in points) - pad,
             min(point.Z for point in points) - pad),
            (max(point.X for point in points) + pad,
             max(point.Y for point in points) + pad,
             max(point.Z for point in points) + pad),
        )
    return ((box.Min.X, box.Min.Y, box.Min.Z),
            (box.Max.X, box.Max.Y, box.Max.Z))


def equipment_label(element):
    """Long label for the pick lists: every name it answers to, plus its id."""
    values = []
    for parameter_name in NAME_PARAMETERS:
        value = _parameter_text(_named_parameter(element, parameter_name))
        if value and value not in values:
            values.append(value)
    comments = get_comments(element)
    if comments:
        values.append("Comments: {}".format(comments))
    type_comments = get_type_comments(element)
    if type_comments:
        values.append("Type Comments: {}".format(type_comments))
    rating = get_isolator_rating(element)
    if rating:
        values.append("JAC-EL-Rating: {}".format(rating))
    try:
        if element.Name and element.Name not in values:
            values.append(element.Name)
    except Exception:
        pass
    try:
        values.append(element.Category.Name)
    except Exception:
        values.append("Equipment")
    values.append("ID {}".format(element_id_value(element.Id)))
    return " | ".join(values)


# =============================================================================
# Panels, circuits and what connects to what
# =============================================================================
def collect_panels():
    """Every electrical equipment item: the panels, boards and transformers."""
    panels = []
    for element in (FilteredElementCollector(doc)
                    .OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                    .WhereElementIsNotElementType()):
        panels.append(element)
    panels.sort(key=lambda item: get_name(item).lower())
    return panels


def collect_isolators():
    """Isolators, wherever they are modelled."""
    found = {}
    for element in (FilteredElementCollector(doc)
                    .OfCategory(BuiltInCategory.OST_ElectricalFixtures)
                    .WhereElementIsNotElementType()):
        if is_isolator(element):
            found[element_id_value(element.Id)] = element
    # Isolators are sometimes modelled in another category, so sweep the
    # electrical categories that could hold one.
    for builtin in (BuiltInCategory.OST_ElectricalEquipment,
                    BuiltInCategory.OST_GenericModel,
                    BuiltInCategory.OST_SpecialityEquipment,
                    BuiltInCategory.OST_MechanicalEquipment):
        try:
            elements = (FilteredElementCollector(doc)
                        .OfCategory(builtin)
                        .WhereElementIsNotElementType()
                        .ToElements())
        except Exception:
            continue
        for element in elements:
            key = element_id_value(element.Id)
            if key in found:
                continue
            if is_isolator(element):
                found[key] = element
    return found


def circuit_details(system):
    """Circuit number, current, voltage and load name of one circuit."""
    details = {
        "number": "", "current": 0.0, "voltage": 0.0, "load": "",
        "rating": 0.0, "poles": 0,
    }
    try:
        details["number"] = system.CircuitNumber or ""
    except Exception:
        pass
    try:
        details["current"] = current_amperes(system.Current)
    except Exception:
        pass
    try:
        details["voltage"] = voltage_volts(system.Voltage)
    except Exception:
        pass
    try:
        details["load"] = system.LoadName or ""
    except Exception:
        pass
    try:
        details["rating"] = current_amperes(system.Rating)
    except Exception:
        pass
    try:
        details["poles"] = system.PolesNumber
    except Exception:
        pass
    return details


def collect_circuit_links():
    """Every circuit as (feeding panel id, member id, circuit details).

    This is the electrical topology already in the model: the panel that
    feeds the circuit, and each element the circuit serves."""
    links = []
    for system in (FilteredElementCollector(doc)
                   .OfClass(ElectricalSystem)
                   .WhereElementIsNotElementType()):
        try:
            base = system.BaseEquipment
        except Exception:
            base = None
        if base is None:
            continue
        base_id = element_id_value(base.Id)
        details = circuit_details(system)
        try:
            members = list(system.Elements)
        except Exception:
            members = []
        for member in members:
            member_id = element_id_value(member.Id)
            if member_id == base_id:
                continue
            links.append((base_id, member_id, details, member))
    return links


def isolators_behind(element_ids, isolators_by_id):
    """Isolators wired to the given elements, following the connectors on.

    An isolator is often between the panel and the plant, so it is reached
    through the equipment rather than from the panel's own circuit."""
    found = set()
    checked = set()
    pending = list(element_ids)
    while pending:
        element_id = pending.pop()
        if element_id in checked:
            continue
        checked.add(element_id)
        try:
            element = doc.GetElement(ElementId(element_id))
        except Exception:
            element = None
        if element is None:
            continue
        for connector in get_element_connectors(element):
            try:
                references = list(connector.AllRefs)
            except Exception:
                continue
            for reference in references:
                try:
                    owner_id = element_id_value(reference.Owner.Id)
                except Exception:
                    continue
                if owner_id in isolators_by_id and owner_id not in found:
                    found.add(owner_id)
                    pending.append(owner_id)
    return found

# =============================================================================
# The cable tray network
# =============================================================================
def orthogonal_distance(start, finish):
    """Across, along and up - how a cable actually gets from A to B."""
    return (abs(start.X - finish.X) + abs(start.Y - finish.Y) +
            abs(start.Z - finish.Z))


def orthogonal_points(start, finish):
    """Right-angled run from start to finish: up, across, then along."""
    return [
        start,
        XYZ(start.X, start.Y, finish.Z),
        XYZ(finish.X, start.Y, finish.Z),
        finish,
    ]


def is_vertical_curve(curve, tolerance):
    try:
        start = curve.GetEndPoint(0)
        end = curve.GetEndPoint(1)
    except Exception:
        return False
    plan_length = math.sqrt((end.X - start.X) ** 2 + (end.Y - start.Y) ** 2)
    return abs(end.Z - start.Z) > tolerance and plan_length <= tolerance


class TrayNetwork(object):
    """Every modelled cable tray centre line, joined into one graph."""

    def __init__(self, tolerance_mm):
        self.tolerance = feet(tolerance_mm)
        self.graph = {}
        self.node_points = {}
        self.node_buckets = {}
        self.trays = {}             # tray id -> (element, curve, length, a, b)
        self.fitting_count = 0
        self.loose_joins = 0
        # Trays bucketed by a coarse grid, so attaching one panel projects
        # onto the trays around it instead of every tray in the building.
        self.cell_size = feet(5000.0)
        self.tray_cells = {}
        self._build()
        self._index_trays()

    # ----- building ----------------------------------------------------------
    def _node_cell(self, point):
        return (int(math.floor(point.X / self.tolerance)),
                int(math.floor(point.Y / self.tolerance)),
                int(math.floor(point.Z / self.tolerance)))

    def node_at(self, point):
        """The node at this point, within the connection tolerance."""
        cell = self._node_cell(point)
        nearest = None
        nearest_distance = self.tolerance
        for delta_x in (-1, 0, 1):
            for delta_y in (-1, 0, 1):
                for delta_z in (-1, 0, 1):
                    bucket = (cell[0] + delta_x, cell[1] + delta_y,
                              cell[2] + delta_z)
                    for node in self.node_buckets.get(bucket, []):
                        distance = point.DistanceTo(self.node_points[node])
                        if distance <= nearest_distance:
                            nearest = node
                            nearest_distance = distance
        if nearest is not None:
            return nearest
        node = len(self.node_points)
        self.node_points[node] = point
        self.node_buckets.setdefault(cell, []).append(node)
        return node

    def add_edge(self, node_a, node_b, weight, edge_data):
        if node_a == node_b:
            return
        self.graph.setdefault(node_a, []).append((node_b, weight, edge_data))
        self.graph.setdefault(node_b, []).append((node_a, weight, edge_data))

    def _build(self):
        trays = list(FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_CableTray)
                     .WhereElementIsNotElementType())
        endpoint_connectors = []
        for tray in trays:
            try:
                curve = tray.Location.Curve
                point_a = curve.GetEndPoint(0)
                point_b = curve.GetEndPoint(1)
            except Exception:
                continue
            node_a = self.node_at(point_a)
            node_b = self.node_at(point_b)
            tray_id = element_id_value(tray.Id)
            self.trays[tray_id] = (tray, curve, curve.Length, node_a, node_b)
            self.add_edge(node_a, node_b, curve.Length, ("tray", tray_id))
            # Ends that merely meet within the tolerance share a node, which
            # is counted here only to report how the network was stitched.
            try:
                for connector in tray.ConnectorManager.Connectors:
                    origin = connector.Origin
                    node = (node_a
                            if origin.DistanceTo(point_a)
                            <= origin.DistanceTo(point_b) else node_b)
                    endpoint_connectors.append((tray_id, node, connector))
            except Exception:
                pass
        self._bridge_fittings(endpoint_connectors)

    def _bridge_fittings(self, endpoint_connectors):
        """A tee or bend joins the tray ends plugged into it."""
        fittings = {}
        for tray_id, node, connector in endpoint_connectors:
            try:
                references = list(connector.AllRefs)
            except Exception:
                continue
            for reference in references:
                try:
                    owner = reference.Owner
                    owner_id = element_id_value(owner.Id)
                except Exception:
                    continue
                if owner_id == tray_id or owner_id in self.trays:
                    continue
                if not is_category(owner,
                                   BuiltInCategory.OST_CableTrayFitting):
                    continue
                try:
                    fittings.setdefault(owner_id, {})[node] = reference.Origin
                except Exception:
                    continue
        for fitting_id, connections in fittings.items():
            items = list(connections.items())
            if len(items) < 2:
                continue
            self.fitting_count += 1
            for index, (node_a, point_a) in enumerate(items):
                for node_b, point_b in items[index + 1:]:
                    edge_points = [
                        self.node_points[node_a], point_a, point_b,
                        self.node_points[node_b]
                    ]
                    length = sum(
                        edge_points[step].DistanceTo(edge_points[step + 1])
                        for step in range(len(edge_points) - 1)
                    )
                    self.add_edge(
                        node_a, node_b, length,
                        ("fitting", fitting_id, node_a, node_b, edge_points)
                    )

    @property
    def ready(self):
        return bool(self.graph)

    # ----- finding the trays near a point ------------------------------------
    def _index_trays(self):
        """Bucket every tray into the grid cells its ends span."""
        for tray_id, data in self.trays.items():
            try:
                start = data[1].GetEndPoint(0)
                end = data[1].GetEndPoint(1)
            except Exception:
                continue
            low = (min(start.X, end.X), min(start.Y, end.Y),
                   min(start.Z, end.Z))
            high = (max(start.X, end.X), max(start.Y, end.Y),
                    max(start.Z, end.Z))
            for cell in self._cells_between(low, high):
                self.tray_cells.setdefault(cell, []).append(tray_id)

    def _cells_between(self, low, high):
        cells = []
        steps = []
        for axis in range(3):
            first = int(math.floor(low[axis] / self.cell_size))
            last = int(math.floor(high[axis] / self.cell_size))
            steps.append(range(first, last + 1))
        for cell_x in steps[0]:
            for cell_y in steps[1]:
                for cell_z in steps[2]:
                    cells.append((cell_x, cell_y, cell_z))
        return cells

    def nearby_trays(self, points, radius):
        """Ids of the trays within 'radius' feet of any of these points."""
        found = set()
        for point in points:
            low = (point.X - radius, point.Y - radius, point.Z - radius)
            high = (point.X + radius, point.Y + radius, point.Z + radius)
            for cell in self._cells_between(low, high):
                found.update(self.tray_cells.get(cell, []))
        return found

    def search_trays(self, points):
        """Trays worth projecting onto: the ones nearby, widening the search
        until something is found (a plant room panel can be a long way from
        the nearest tray)."""
        if not self.trays:
            return []
        for radius_mm in (5000.0, 15000.0, 40000.0, 100000.0):
            found = self.nearby_trays(points, feet(radius_mm))
            if found:
                return list(found)
        return list(self.trays)

    # ----- attaching equipment ----------------------------------------------
    def attach(self, equipment_points, maximum=MAX_ATTACH_CANDIDATES):
        """Where this equipment joins the network, cheapest first.

        For each tray the point is projected onto, both ends of that tray are
        offered: the cable runs orthogonally to the tray, then along it to
        whichever end the route needs."""
        candidates = []
        for tray_id in self.search_trays(equipment_points):
            data = self.trays[tray_id]
            curve = data[1]
            for equipment_point in equipment_points:
                try:
                    projection = curve.Project(equipment_point)
                except Exception:
                    continue
                if projection is None:
                    continue
                try:
                    tray_point = projection.XYZPoint
                    parameter = projection.Parameter
                except Exception:
                    continue
                gap = orthogonal_distance(equipment_point, tray_point)
                for endpoint_index in (0, 1):
                    try:
                        end_parameter = curve.GetEndParameter(endpoint_index)
                    except Exception:
                        continue
                    if abs(parameter - end_parameter) < 1e-9:
                        partial_length = 0.0
                        partial_points = [tray_point]
                    else:
                        try:
                            partial = curve.Clone()
                            partial.MakeBound(min(parameter, end_parameter),
                                              max(parameter, end_parameter))
                            partial_length = partial.Length
                            partial_points = list(partial.Tessellate())
                        except Exception:
                            continue
                        if parameter > end_parameter:
                            partial_points.reverse()
                    candidates.append({
                        "cost": gap + partial_length,
                        "node": data[3] if endpoint_index == 0 else data[4],
                        "gap": gap,
                        "partial_length": partial_length,
                        "equipment_point": equipment_point,
                        "tray_point": tray_point,
                        "tray_id": tray_id,
                        "partial_points": partial_points,
                        "vertical": is_vertical_curve(curve, self.tolerance),
                    })
        candidates = self._prefer_risers(candidates, equipment_points)
        candidates = self._nearest_drop_only(candidates)
        if not candidates:
            candidates = self._nearest_ends(equipment_points)
        candidates.sort(key=lambda item: item["cost"])
        chosen = []
        seen_nodes = set()
        for candidate in candidates:
            if candidate["node"] in seen_nodes:
                continue
            seen_nodes.add(candidate["node"])
            chosen.append(candidate)
            if len(chosen) >= maximum:
                break
        return chosen

    def _prefer_risers(self, candidates, equipment_points):
        """Equipment on another level should drop onto a riser, not fly to a
        tray overhead on a different floor."""
        if not candidates:
            return candidates
        kept = []
        for equipment_point in equipment_points:
            mine = [candidate for candidate in candidates
                    if candidate["equipment_point"] is equipment_point]
            risers = []
            same_level = []
            off_level = []
            for candidate in mine:
                difference = abs(
                    equipment_point.Z - candidate["tray_point"].Z)
                if candidate["vertical"] and difference <= self.tolerance:
                    risers.append(candidate)
                elif difference <= self.tolerance:
                    same_level.append(candidate)
                else:
                    off_level.append(candidate)
            if (risers and off_level and not same_level
                    and min(item["gap"] for item in risers)
                    < min(item["gap"] for item in off_level)):
                kept.extend(risers)
            else:
                kept.extend(mine)
        return kept

    def _nearest_drop_only(self, candidates):
        """Keep the attachments at the nearest tray, both ends of it.

        Each candidate is 'drop onto this tray, then run along it to this
        end'. Only the trays the cable could really drop onto are kept, so
        the route along the trays is found by the graph rather than hidden
        inside an attachment cost."""
        if not candidates:
            return candidates
        closest = min(candidate["gap"] for candidate in candidates)
        reach = closest + feet(ATTACH_GAP_SLACK_MM)
        return [candidate for candidate in candidates
                if candidate["gap"] <= reach]

    def _nearest_ends(self, equipment_points):
        """Last resort: straight to the nearest tray end."""
        candidates = []
        for tray_id, data in self.trays.items():
            for endpoint_index, node in ((0, data[3]), (1, data[4])):
                tray_point = self.node_points[node]
                for equipment_point in equipment_points:
                    gap = orthogonal_distance(equipment_point, tray_point)
                    candidates.append({
                        "cost": gap,
                        "node": node,
                        "gap": gap,
                        "partial_length": 0.0,
                        "equipment_point": equipment_point,
                        "tray_point": tray_point,
                        "tray_id": tray_id,
                        "partial_points": [tray_point],
                        "vertical": False,
                    })
        return candidates

    # ----- distances ---------------------------------------------------------
    def distances_from(self, candidates):
        """One Dijkstra pass for all of an item's attachment points.

        Each candidate seeds the queue with its own attachment cost, so the
        result is the cheapest way out of that panel to every node in the
        network - and every other panel is then a dictionary lookup."""
        distances = {}
        previous = {}
        seeds = {}
        queue = []
        for candidate in candidates:
            node = candidate["node"]
            cost = candidate["cost"]
            if cost < distances.get(node, float("inf")):
                distances[node] = cost
                seeds[node] = node
                heapq.heappush(queue, (cost, node))
        while queue:
            current, node = heapq.heappop(queue)
            if current > distances.get(node, float("inf")) + EPSILON:
                continue
            for neighbour, weight, edge_data in self.graph.get(node, []):
                candidate_cost = current + weight
                if candidate_cost < distances.get(neighbour, float("inf")):
                    distances[neighbour] = candidate_cost
                    previous[neighbour] = (node, edge_data)
                    seeds[neighbour] = seeds[node]
                    heapq.heappush(queue, (candidate_cost, neighbour))
        return {"distances": distances, "previous": previous, "seeds": seeds}

    def path_to(self, reach, node):
        """(nodes, edges) from the seed attachment node to this node."""
        previous = reach["previous"]
        nodes = [node]
        edges = []
        cursor = node
        guard = 0
        while cursor in previous:
            prior, edge_data = previous[cursor]
            edges.append(edge_data)
            nodes.append(prior)
            cursor = prior
            guard += 1
            if guard > 1000000:
                break
        nodes.reverse()
        edges.reverse()
        return nodes, edges

    def edge_points(self, path_nodes, path_edges):
        """The tray centre lines the route follows, in order."""
        points = []
        for index, edge_data in enumerate(path_edges):
            if edge_data[0] == "tray":
                tray_id = edge_data[1]
                data = self.trays.get(tray_id)
                if data is None:
                    continue
                try:
                    edge = list(data[1].Tessellate())
                except Exception:
                    continue
                if path_nodes[index] != data[3]:
                    edge.reverse()
            else:
                edge = list(edge_data[4])
                if path_nodes[index] != edge_data[2]:
                    edge.reverse()
            points.extend(edge)
        return points


def route_polyline(network, source_candidate, destination_candidate,
                   path_nodes, path_edges, bridge):
    """Every point of the drawn route, source equipment to destination."""
    points = orthogonal_points(source_candidate["equipment_point"],
                               source_candidate["tray_point"])
    points.extend(source_candidate["partial_points"])
    points.extend(network.edge_points(path_nodes, path_edges))
    if bridge:
        points.extend(orthogonal_points(
            network.node_points[source_candidate["node"]],
            network.node_points[destination_candidate["node"]]
        )[1:])
    points.extend(reversed(destination_candidate["partial_points"]))
    points.extend(orthogonal_points(
        destination_candidate["tray_point"],
        destination_candidate["equipment_point"]
    ))
    return points


def measure_route(network, source_reach, source_candidates,
                  destination_candidates):
    """Cheapest route from one attached item to another.

    Returns the lengths, the points to draw and whether the trays had to be
    bridged because the modelled network is in pieces."""
    by_node = {}
    for candidate in source_candidates:
        by_node[candidate["node"]] = candidate
    distances = source_reach["distances"]
    seeds = source_reach["seeds"]
    best = None
    for destination in destination_candidates:
        node = destination["node"]
        if node not in distances:
            continue
        total = distances[node] + destination["cost"]
        if best is None or total < best[0]:
            best = (total, destination)
    if best is not None:
        total, destination = best
        node = destination["node"]
        source_candidate = by_node.get(seeds.get(node))
        if source_candidate is not None:
            path_nodes, path_edges = network.path_to(source_reach, node)
            network_length = (distances[node] - source_candidate["cost"])
            return {
                "total": total,
                "network_length": max(network_length, 0.0),
                "source": source_candidate,
                "destination": destination,
                "path_nodes": path_nodes,
                "path_edges": path_edges,
                "bridge": 0.0,
                "points": route_polyline(
                    network, source_candidate, destination,
                    path_nodes, path_edges, False
                ),
            }
    # The trays do not join up: keep both tray legs and bridge the gap.
    fallback = None
    for source_candidate in source_candidates:
        for destination in destination_candidates:
            gap = orthogonal_distance(
                network.node_points[source_candidate["node"]],
                network.node_points[destination["node"]]
            )
            total = source_candidate["cost"] + gap + destination["cost"]
            # Several combinations usually come to the same total. Prefer the
            # one with the shortest bridge, so the gap reported is the real
            # break in the trays rather than a diagonal across the building.
            # The totals are compared with a tolerance: they are sums of
            # floats and never match to the last bit.
            if fallback is None:
                fallback = (total, source_candidate, destination, gap)
                continue
            tie = feet(TIE_TOLERANCE_MM)
            if (total < fallback[0] - tie
                    or (total < fallback[0] + tie and gap < fallback[3])):
                fallback = (total, source_candidate, destination, gap)
    if fallback is None:
        return None
    total, source_candidate, destination, gap = fallback
    return {
        "total": total,
        "network_length": 0.0,
        "source": source_candidate,
        "destination": destination,
        "path_nodes": [source_candidate["node"], destination["node"]],
        "path_edges": [],
        "bridge": gap,
        "points": route_polyline(
            network, source_candidate, destination,
            [source_candidate["node"], destination["node"]], [], True
        ),
    }

# =============================================================================
# Building the route list
# =============================================================================
ROUTE_KIND_NAMES = {
    "circuit": "circuit",
    "isolator": "isolator",
    "pair": "panel pair",
}


def voltage_drop_volts(length_metres, current, mv_per_a_m):
    """drop = mV/A/m x current x length / 1000, the usual cable-table sum."""
    return mv_per_a_m * current * length_metres / 1000.0


def make_route(kind, source, destination, measurement, values, circuit=None):
    """One row of the results: lengths, current and voltage drop."""
    source_candidate = measurement["source"]
    destination_candidate = measurement["destination"]
    tray_length = (source_candidate["partial_length"]
                   + measurement["network_length"]
                   + destination_candidate["partial_length"])
    total = measurement["total"]
    with_allowance = total * (1.0 + values["allowance"] / 100.0)
    current = values["current"]
    voltage = values["voltage"]
    circuit_number = ""
    load_name = ""
    from_circuit = False
    if circuit:
        circuit_number = circuit.get("number") or ""
        load_name = circuit.get("load") or ""
        if circuit.get("current"):
            current = circuit["current"]
            from_circuit = True
        if circuit.get("voltage"):
            voltage = circuit["voltage"]
    length_metres = metres(with_allowance)
    drop = voltage_drop_volts(length_metres, current, values["mv_per_a_m"])
    percent = (drop / voltage * 100.0) if voltage > EPSILON else 0.0
    source_point = source_candidate["equipment_point"]
    destination_point = destination_candidate["equipment_point"]
    return {
        "kind": kind,
        "source": source,
        "source_id": element_id_value(source.Id),
        "source_name": get_name(source),
        "source_level": get_level_name(source),
        "destination": destination,
        "destination_id": element_id_value(destination.Id),
        "destination_name": get_name(destination),
        "destination_level": get_level_name(destination),
        "circuit_number": circuit_number,
        "load_name": load_name,
        "current": current,
        "current_from_circuit": from_circuit,
        "voltage": voltage,
        "tray_length": tray_length,
        "source_gap": source_candidate["gap"],
        "destination_gap": destination_candidate["gap"],
        "bridge": measurement["bridge"],
        "total": total,
        "with_allowance": with_allowance,
        "vertical": destination_point.Z - source_point.Z,
        "drop_volts": drop,
        "drop_percent": percent,
        "within_limit": percent <= values["limit_percent"] + 1e-9,
        "points": measurement["points"],
        "include": True,
    }


def route_title(route):
    return u"{}  ->  {}".format(route["source_name"],
                                route["destination_name"])


def build_routes(network, values, panels, isolators_by_id, scope_ids):
    """Measure every route the scope asks for.

    One Dijkstra pass per panel serves all of that panel's destinations, and
    its distance map is dropped before the next panel, so a whole building
    costs one pass per panel instead of one per pair."""
    attachments = {}
    unreachable = []
    cancelled = False

    def candidates_for(element):
        key = element_id_value(element.Id)
        if key not in attachments:
            points = get_points(element)
            attachments[key] = network.attach(points) if points else []
        return attachments[key]

    links_by_base = {}
    circuit_couples = set()
    if values["scope_circuits"] or values["scope_isolators"]:
        for base_id, member_id, details, member in collect_circuit_links():
            links_by_base.setdefault(base_id, []).append(
                (member_id, details, member)
            )
            circuit_couples.add(frozenset((base_id, member_id)))

    in_scope = [panel for panel in panels
                if element_id_value(panel.Id) in scope_ids]
    maximum_pair = values.get("max_pair_metres") or 0.0
    routes = []
    seen_couples = set()

    total_panels = max(len(in_scope), 1)
    progress = None
    try:
        progress = forms.ProgressBar(
            title="Measuring cable routes  ({value} of {max_value} panels)",
            cancellable=True
        )
        progress.__enter__()
    except Exception:
        progress = None

    try:
        for index, panel in enumerate(in_scope):
            if progress is not None:
                try:
                    progress.update_progress(index + 1, total_panels)
                    if progress.cancelled:
                        cancelled = True
                        break
                except Exception:
                    pass
            panel_id = element_id_value(panel.Id)
            source_candidates = candidates_for(panel)
            if not source_candidates:
                unreachable.append((panel, "no cable tray to attach to"))
                continue
            reach = network.distances_from(source_candidates)

            # --- the equipment on this panel's circuits ---------------------
            circuit_members = []
            for member_id, details, member in links_by_base.get(panel_id, []):
                circuit_members.append(member_id)
                if not values["scope_circuits"]:
                    continue
                if member is None:
                    continue
                if (not values["scope_isolators"]
                        and is_isolator(member)):
                    continue
                couple = (panel_id, member_id, "circuit")
                if couple in seen_couples:
                    continue
                destination_candidates = candidates_for(member)
                if not destination_candidates:
                    unreachable.append(
                        (member, "no cable tray to attach to"))
                    continue
                measurement = measure_route(
                    network, reach, source_candidates, destination_candidates
                )
                if measurement is None:
                    unreachable.append((member, "no route through the trays"))
                    continue
                seen_couples.add(couple)
                routes.append(make_route(
                    "circuit", panel, member, measurement, values, details
                ))

            # --- isolators wired behind that equipment ----------------------
            if values["scope_isolators"] and isolators_by_id:
                behind = isolators_behind(circuit_members, isolators_by_id)
                for isolator_id in sorted(behind):
                    couple = (panel_id, isolator_id, "isolator")
                    if couple in seen_couples:
                        continue
                    isolator = isolators_by_id[isolator_id]
                    destination_candidates = candidates_for(isolator)
                    if not destination_candidates:
                        unreachable.append(
                            (isolator, "no cable tray to attach to"))
                        continue
                    measurement = measure_route(
                        network, reach, source_candidates,
                        destination_candidates
                    )
                    if measurement is None:
                        unreachable.append(
                            (isolator, "no route through the trays"))
                        continue
                    seen_couples.add(couple)
                    routes.append(make_route(
                        "isolator", panel, isolator, measurement, values
                    ))

            # --- panel to panel ---------------------------------------------
            if values["scope_pairs"]:
                for other in in_scope[index + 1:]:
                    other_id = element_id_value(other.Id)
                    if frozenset((panel_id, other_id)) in circuit_couples:
                        continue        # already measured as a circuit
                    destination_candidates = candidates_for(other)
                    if not destination_candidates:
                        continue
                    measurement = measure_route(
                        network, reach, source_candidates,
                        destination_candidates
                    )
                    if measurement is None:
                        continue
                    if (maximum_pair > 0.0
                            and metres(measurement["total"]) > maximum_pair):
                        continue
                    routes.append(make_route(
                        "pair", panel, other, measurement, values
                    ))
            reach = None        # free the distance map before the next panel
    finally:
        if progress is not None:
            try:
                progress.__exit__(None, None, None)
            except Exception:
                pass

    routes.sort(key=lambda item: (-item["drop_percent"], -item["total"]))
    return {
        "routes": routes,
        "unreachable": unreachable,
        "cancelled": cancelled,
        "panels": in_scope,
    }


def panel_rows(routes, panels):
    """One row per panel for the Panels tab."""
    rows = {}
    for panel in panels:
        rows[element_id_value(panel.Id)] = {
            "panel": panel,
            "name": get_name(panel),
            "level": get_level_name(panel),
            "count": 0,
            "longest": 0.0,
            "worst_percent": 0.0,
            "over_limit": 0,
            "bridged": 0,
        }
    for route in routes:
        for key in ("source_id", "destination_id"):
            row = rows.get(route[key])
            if row is None:
                continue
            row["count"] += 1
            row["longest"] = max(row["longest"], route["with_allowance"])
            row["worst_percent"] = max(row["worst_percent"],
                                       route["drop_percent"])
            if not route["within_limit"]:
                row["over_limit"] += 1
            if route["bridge"] > EPSILON:
                row["bridged"] += 1
    ordered = sorted(rows.values(),
                     key=lambda item: (-item["worst_percent"],
                                       item["name"].lower()))
    return [row for row in ordered if row["count"]] + \
           [row for row in ordered if not row["count"]]

# =============================================================================
# Export (CSV, and Excel through the CSV when Excel is installed)
# =============================================================================
EXPORT_HEADER = (
    "Connection", "Source panel", "Source level", "Destination",
    "Destination level", "Circuit", "Load name", "Tray route (m)",
    "Source connection (m)", "Destination connection (m)",
    "Bridged tray gap (m)", "Route length (m)", "With allowance (m)",
    "Vertical difference (m)", "Current (A)", "Current from", "Voltage (V)",
    "mV/A/m", "Voltage drop (V)", "Voltage drop (%)", "Limit (%)", "Status",
    "Source ID", "Destination ID",
)


def export_rows(routes, values):
    """The routes as rows of plain values, ready for CSV or Excel."""
    rows = []
    for route in routes:
        rows.append((
            ROUTE_KIND_NAMES.get(route["kind"], route["kind"]),
            route["source_name"],
            route["source_level"],
            route["destination_name"],
            route["destination_level"],
            route["circuit_number"],
            route["load_name"],
            round(metres(route["tray_length"]), 3),
            round(metres(route["source_gap"]), 3),
            round(metres(route["destination_gap"]), 3),
            round(metres(route["bridge"]), 3),
            round(metres(route["total"]), 3),
            round(metres(route["with_allowance"]), 3),
            round(metres(route["vertical"]), 3),
            round(route["current"], 2),
            "Revit circuit" if route["current_from_circuit"] else "dialog",
            round(route["voltage"], 1),
            values["mv_per_a_m"],
            round(route["drop_volts"], 2),
            round(route["drop_percent"], 3),
            values["limit_percent"],
            "OK" if route["within_limit"] else "OVER LIMIT",
            route["source_id"],
            route["destination_id"],
        ))
    return rows


def csv_value(value):
    """One CSV field, quoted when it has to be."""
    if value is None:
        text = u""
    elif isinstance(value, float):
        text = u"{:g}".format(value)
    else:
        text = u"{}".format(value)
    if any(character in text for character in (u",", u'"', u"\n", u"\r")):
        return u'"{}"'.format(text.replace(u'"', u'""'))
    return text


def csv_text(header, rows):
    lines = [u",".join(csv_value(item) for item in header)]
    for row in rows:
        lines.append(u",".join(csv_value(item) for item in row))
    return u"\r\n".join(lines) + u"\r\n"


def write_csv(path, header, rows):
    """UTF-8 with a byte-order mark, so Excel reads the accents correctly."""
    import codecs
    stream = codecs.open(path, "w", "utf-8-sig")
    try:
        stream.write(csv_text(header, rows))
    finally:
        stream.close()
    return path


def default_export_name(suffix="csv"):
    name = "Panel cable routes"
    try:
        if doc.Title:
            name = "{} - panel cable routes".format(doc.Title)
    except Exception:
        pass
    return "{}.{}".format(name.replace(":", "-"), suffix)


def ask_export_path():
    """Where to write the file. None when the user cancels."""
    try:
        return forms.save_file(
            file_ext="csv", default_name=default_export_name("csv")
        )
    except Exception:
        pass
    import os
    try:
        folder = os.path.expanduser("~")
        return os.path.join(folder, default_export_name("csv"))
    except Exception:
        return None


def open_in_excel(path, save_as_xlsx=True):
    """Open the exported CSV in Excel and dress it up a little.

    Excel is driven through late-bound COM, so no Interop assembly has to be
    installed. Every step is optional: if one is not allowed, the workbook is
    still open with the data in it."""
    from System import Activator, Type
    excel_type = Type.GetTypeFromProgID("Excel.Application")
    if excel_type is None:
        raise Exception("Excel is not installed on this machine.")
    excel = Activator.CreateInstance(excel_type)
    excel.Visible = True
    workbook = excel.Workbooks.Open(path)
    sheet = workbook.ActiveSheet
    for step in (
            lambda: sheet.Rows[1].Font.__setattr__("Bold", True),
            lambda: sheet.UsedRange.AutoFilter(),
            lambda: sheet.Columns.AutoFit(),
            lambda: excel.ActiveWindow.__setattr__("SplitRow", 1),
            lambda: excel.ActiveWindow.__setattr__("FreezePanes", True)):
        try:
            step()
        except Exception:
            pass
    saved_as = None
    if save_as_xlsx:
        try:
            xlsx_path = path[:-4] + ".xlsx" if path.lower().endswith(".csv") \
                else path + ".xlsx"
            # 51 = xlOpenXMLWorkbook (.xlsx)
            workbook.SaveAs(xlsx_path, 51)
            saved_as = xlsx_path
        except Exception:
            saved_as = None
    return saved_as


def report_lines(result, values, network):
    """The Overview tab, and the text that goes on the clipboard."""
    routes = result["routes"]
    over = [route for route in routes if not route["within_limit"]]
    bridged = [route for route in routes if route["bridge"] > EPSILON]
    circuits = [route for route in routes if route["kind"] == "circuit"]
    isolators = [route for route in routes if route["kind"] == "isolator"]
    pairs = [route for route in routes if route["kind"] == "pair"]
    lines = [
        "Panels measured: {}".format(len(result["panels"])),
        "Routes: {}  ({} circuit, {} isolator, {} panel pair)".format(
            len(routes), len(circuits), len(isolators), len(pairs)
        ),
        "",
        "Tray network: {} tray(s), {} node(s), {} fitting(s) bridging "
        "tray ends, {:g} mm connection tolerance.".format(
            len(network.trays), len(network.node_points),
            network.fitting_count, values["tolerance"]
        ),
        "",
        "Cable allowance: {:g} %  (the drop is worked out on the length "
        "with allowance)".format(values["allowance"]),
        "Cable: {:g} mV/A/m, limit {:g} % of the nominal voltage".format(
            values["mv_per_a_m"], values["limit_percent"]
        ),
        "Fallbacks when a circuit has no figures: {:g} A, {:g} V".format(
            values["current"], values["voltage"]
        ),
        "",
    ]
    if routes:
        longest = max(routes, key=lambda item: item["with_allowance"])
        worst = max(routes, key=lambda item: item["drop_percent"])
        lines.append("Longest route: {:.2f} m  {}".format(
            metres(longest["with_allowance"]), route_title(longest)
        ))
        lines.append("Worst voltage drop: {:.2f} %  ({:.1f} V on {:.0f} V)  "
                     "{}".format(
                         worst["drop_percent"], worst["drop_volts"],
                         worst["voltage"], route_title(worst)
                     ))
        lines.append("Over the {:g} % limit: {} route(s)".format(
            values["limit_percent"], len(over)
        ))
    lines.append("")
    lines.append("Checks")
    lines.append("  Equipment not reached : {}".format(
        len(result["unreachable"]) or "none"
    ))
    lines.append("  Routes bridging a gap in the modelled trays: {}".format(
        len(bridged) or "none"
    ))
    if result["cancelled"]:
        lines.append("")
        lines.append("The measuring run was cancelled, so this is a partial "
                     "set of routes.")
    return lines

# =============================================================================
# 3D PREVIEW GEOMETRY (WPF 3D - nothing is drawn in the Revit model)
# =============================================================================
# The tray network, the panels and every measured route are drawn as boxes in
# a viewport inside the dialog: routes as thin tubes along their real points,
# trays at their real width and height, panels as their bounding boxes.
#
# Colours (keep in step with the legend swatches in RESULT_XAML).
VIEW_COLOURS = {
    "tray": (80, 148, 163, 184),        # the modelled tray network, grey
    "panel": (235, 37, 99, 235),        # panels and equipment, blue
    "route": (210, 34, 211, 238),       # a measured route, cyan
    "over": (235, 239, 68, 68),         # over the voltage-drop limit, red
    "selected": (255, 250, 204, 21),    # the route picked in the table
}

PRESET_VIEWS = {
    "iso": (35.0, 26.0),
    "top": (90.0, 88.0),
    "front": (0.0, 3.0),
    "side": (90.0, 3.0),
}

BOX_TRIANGLES = (
    (0, 1, 5), (0, 5, 4),
    (3, 7, 6), (3, 6, 2),
    (0, 4, 7), (0, 7, 3),
    (1, 2, 6), (1, 6, 5),
    (0, 3, 2), (0, 2, 1),
    (4, 5, 6), (4, 6, 7),
)

ROUTE_TUBE_MM = 45.0
SELECTED_TUBE_MM = 110.0
DEFAULT_TRAY_MM = 100.0
MAX_SCENE_BOXES = 24000


def media_brush(argb):
    return SolidColorBrush(MediaColor.FromArgb(*argb))


def vector_cross(first, second):
    return (first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0])


def vector_unit(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1e-12:
        return None
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def vector_scale(vector, factor):
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def point_tuple(point):
    return (point.X, point.Y, point.Z)


def prism_corners(start, end, normal, up):
    """The 8 corners of a box running start -> end."""
    corners = []
    for point in (start, end):
        for sign_normal, sign_up in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            corners.append((
                point[0] + sign_normal * normal[0] + sign_up * up[0],
                point[1] + sign_normal * normal[1] + sign_up * up[1],
                point[2] + sign_normal * normal[2] + sign_up * up[2],
            ))
    return corners


def axis_box_corners(low, high):
    """The 8 corners of an axis-aligned box, from two (x, y, z) tuples."""
    middle_y = (low[1] + high[1]) / 2.0
    middle_z = (low[2] + high[2]) / 2.0
    return prism_corners(
        (low[0], middle_y, middle_z), (high[0], middle_y, middle_z),
        (0.0, (high[1] - low[1]) / 2.0, 0.0),
        (0.0, 0.0, (high[2] - low[2]) / 2.0)
    )


def tube_corners(start, end, half_width, half_height=None):
    """A square tube from start to end, for drawing a line with thickness."""
    direction = vector_unit((end[0] - start[0], end[1] - start[1],
                             end[2] - start[2]))
    if direction is None:
        return None
    helper = (0.0, 0.0, 1.0) if abs(direction[2]) < 0.9 else (1.0, 0.0, 0.0)
    normal = vector_unit(vector_cross(direction, helper))
    if normal is None:
        return None
    up = vector_unit(vector_cross(normal, direction)) or (0.0, 0.0, 1.0)
    if half_height is None:
        half_height = half_width
    return prism_corners(start, end, vector_scale(normal, half_width),
                         vector_scale(up, half_height))


def add_box(mesh, corners):
    """Append one box to a mesh, keeping everything already in it."""
    base = mesh.Positions.Count
    for corner in corners:
        mesh.Positions.Add(Point3D(corner[0], corner[1], corner[2]))
    for triangle in BOX_TRIANGLES:
        for index in triangle:
            mesh.TriangleIndices.Add(base + index)


def add_polyline(mesh, points, half_width, cloud=None, limit=None):
    """Draw a run of points as a chain of tubes. Returns the boxes added."""
    added = 0
    previous = None
    for point in points:
        current = point_tuple(point) if hasattr(point, "X") else point
        if previous is not None:
            step = math.sqrt(sum(
                (current[axis] - previous[axis]) ** 2 for axis in range(3)
            ))
            if step > half_width:
                corners = tube_corners(previous, current, half_width)
                if corners is not None:
                    add_box(mesh, corners)
                    added += 1
                    if cloud is not None:
                        cloud.extend(corners)
                    if limit is not None and added >= limit:
                        return added
        previous = current
    return added


def shaded_model(mesh, argb):
    """A mesh painted on both faces, so no box can look inside out."""
    material = DiffuseMaterial(media_brush(argb))
    model = GeometryModel3D(mesh, material)
    model.BackMaterial = material
    return model


def tray_prism(data):
    """A tray drawn at its real width and height."""
    tray, curve = data[0], data[1]
    try:
        start = point_tuple(curve.GetEndPoint(0))
        end = point_tuple(curve.GetEndPoint(1))
    except Exception:
        return None
    width = DEFAULT_TRAY_MM
    height = DEFAULT_TRAY_MM
    try:
        parameter = tray.get_Parameter(
            BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM)
        if parameter is not None:
            width = max(parameter.AsDouble() / MM_TO_FT, 1.0)
        parameter = tray.get_Parameter(
            BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM)
        if parameter is not None:
            height = max(parameter.AsDouble() / MM_TO_FT, 1.0)
    except Exception:
        pass
    return tube_corners(start, end, feet(width) / 2.0, feet(height) / 2.0)

# =============================================================================
# SETUP DIALOG (WPF)
# =============================================================================
SETUP_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Panel Cable Route Pro" Width="900" SizeToContent="Height"
        ResizeMode="NoResize" WindowStartupLocation="CenterScreen"
        ShowInTaskbar="False" Background="#F1F4F9"
        FontFamily="Segoe UI" FontSize="12" Foreground="#1E293B"
        UseLayoutRounding="True">
  <Window.Resources>
    <Style x:Key="Card" TargetType="Border">
      <Setter Property="Background" Value="White"/>
      <Setter Property="BorderBrush" Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="10"/>
      <Setter Property="Padding" Value="18,16"/>
      <Setter Property="Margin" Value="0,0,0,14"/>
    </Style>
    <Style x:Key="Badge" TargetType="Border">
      <Setter Property="Width" Value="24"/>
      <Setter Property="Height" Value="24"/>
      <Setter Property="CornerRadius" Value="12"/>
      <Setter Property="Background" Value="#DBEAFE"/>
      <Setter Property="Margin" Value="0,0,10,0"/>
    </Style>
    <Style x:Key="BadgeText" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#1D4ED8"/>
      <Setter Property="FontWeight" Value="Bold"/>
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="CardTitle" TargetType="TextBlock">
      <Setter Property="FontSize" Value="14"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Foreground" Value="#0F172A"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="Label" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#334155"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Margin" Value="0,0,0,5"/>
    </Style>
    <Style x:Key="Hint" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="Margin" Value="0,5,0,0"/>
    </Style>
    <Style TargetType="CheckBox">
      <Setter Property="Margin" Value="0,0,0,9"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Height" Value="32"/>
      <Setter Property="Padding" Value="8,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="BorderBrush" Value="#CBD5E1"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TextBox">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="1" CornerRadius="6">
              <ScrollViewer x:Name="PART_ContentHost"
                            Margin="{TemplateBinding Padding}"
                            VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="BorderBrush" Value="#93C5FD"/>
              </Trigger>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter TargetName="bd" Property="BorderBrush" Value="#2563EB"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="PrimaryButton" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="Background" Value="#2563EB"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Height" Value="36"/>
      <Setter Property="Padding" Value="20,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#1D4ED8"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SecondaryButton" TargetType="Button">
      <Setter Property="Foreground" Value="#334155"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Height" Value="36"/>
      <Setter Property="Padding" Value="16,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="#CBD5E1" BorderThickness="1"
                    CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#F1F5F9"/>
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
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <Border Grid.Row="0" Padding="24,18">
      <Border.Background>
        <LinearGradientBrush StartPoint="0,0" EndPoint="1,0">
          <GradientStop Color="#1E3A8A" Offset="0"/>
          <GradientStop Color="#2563EB" Offset="1"/>
        </LinearGradientBrush>
      </Border.Background>
      <DockPanel>
        <Border DockPanel.Dock="Left" Width="46" Height="46" CornerRadius="10"
                Background="#33FFFFFF" Margin="0,0,14,0">
          <Path Stroke="White" StrokeThickness="2.4" StrokeLineJoin="Round"
                StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Data="M 3,20 L 3,8 L 11,8 L 11,20 M 19,20 L 19,8 L 27,8 L 27,20 M 7,14 L 23,14 M 23,11 L 23,17"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="Panel Cable Route Pro" FontSize="20"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock Text="Panel-to-panel cable tray lengths for voltage-drop calculations"
                     FontSize="12" Foreground="#DBEAFE" Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto"
                  HorizontalScrollBarVisibility="Disabled">
    <Grid Margin="20,18,20,4">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="16"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>

      <StackPanel Grid.Column="0">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,14">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="1" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="What to measure"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <CheckBox x:Name="cbPairs"
                      ToolTip="Measure the tray route between every pair of panels in scope. This is what gives you a distance for any panel to any panel.">
              <TextBlock Text="Panel to panel - every pair"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <CheckBox x:Name="cbCircuits"
                      ToolTip="Measure from each panel to the equipment on its own circuits, using the circuit's current and voltage for the drop.">
              <TextBlock Text="Panel to the equipment on its circuits"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <CheckBox x:Name="cbIsolators"
                      ToolTip="Also measure to isolators - tagged in Comments or JAC-EL-Rating, or named one - including those wired behind the equipment.">
              <TextBlock Text="Include isolators" FontWeight="SemiBold"
                         Foreground="#334155"/>
            </CheckBox>
            <Grid Margin="0,4,0,0">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <StackPanel Grid.Column="0">
                <TextBlock Text="Panels in scope" Style="{StaticResource Label}"/>
                <Border Background="#F8FAFC" BorderBrush="#E2E8F0"
                        BorderThickness="1" CornerRadius="6" Padding="10,8"
                        MinHeight="32">
                  <TextBlock x:Name="tbPanelScope" TextWrapping="Wrap"
                             Foreground="#334155" VerticalAlignment="Center"/>
                </Border>
              </StackPanel>
              <Button x:Name="btnChoosePanels" Grid.Column="2"
                      Content="Choose..." VerticalAlignment="Bottom"
                      Style="{StaticResource SecondaryButton}"/>
            </Grid>
            <TextBlock Style="{StaticResource Hint}"
                       Text="Every pair of N panels is N x (N-1) / 2 routes. Measuring is one pass per panel, not one per pair, but a very large job is still quicker with a shorter list."/>
            <TextBlock Text="Ignore pairs longer than (m)" Margin="0,10,0,5"
                       Style="{StaticResource Label}"/>
            <TextBox x:Name="tbMaxPair"
                     ToolTip="Drops panel pairs whose route is longer than this, to keep the table to the connections that could actually be cabled. 0 keeps every pair."/>
          </StackPanel>
        </Border>

        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,14">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="3" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Tray network and output"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <TextBlock Text="Tray connection tolerance (mm)"
                       Style="{StaticResource Label}"/>
            <TextBox x:Name="tbTolerance"
                     ToolTip="Tray ends this far apart are treated as connected. Raise it when the model has small gaps between tray runs; lower it to measure only what is truly joined."/>
            <TextBlock Style="{StaticResource Hint}" Margin="0,5,0,12"
                       Text="Routes follow the modelled tray centre lines. Where the trays do not join up, the gap is bridged orthogonally and the route is flagged."/>
            <CheckBox x:Name="cbDraw" Margin="0,0,0,0"
                      ToolTip="After the preview you can draw the ticked routes as red detail lines in the plan views of both end levels.">
              <TextBlock Text="Offer to draw red detail lines"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
          </StackPanel>
        </Border>
      </StackPanel>

      <StackPanel Grid.Column="2">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,14">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="2" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Cable and voltage drop"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <Grid Margin="0,0,0,12">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <StackPanel Grid.Column="0">
                <TextBlock Text="Cable allowance (%)"
                           Style="{StaticResource Label}"/>
                <TextBox x:Name="tbAllowance"
                         ToolTip="Added to the measured route for terminations, slack and drops. The voltage drop is worked out on the length including this allowance."/>
              </StackPanel>
              <StackPanel Grid.Column="2">
                <TextBlock Text="Cable mV/A/m" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbMvAm"
                         ToolTip="The figure from the cable manufacturer's table for the size you intend to use. It is a property of the cable, not of the model, so it is entered here."/>
              </StackPanel>
            </Grid>
            <Grid Margin="0,0,0,12">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <StackPanel Grid.Column="0">
                <TextBlock Text="Limit (%)" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbLimit"
                         ToolTip="Routes whose drop is over this are flagged in the table and the export. 5% total, or 3% for a final circuit, is the usual figure."/>
              </StackPanel>
              <StackPanel Grid.Column="2">
                <TextBlock Text="Current (A)" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbCurrent"
                         ToolTip="Used when the connection is not a Revit circuit, or the circuit carries no current. Circuit currents are read from the model and take precedence."/>
              </StackPanel>
              <StackPanel Grid.Column="4">
                <TextBlock Text="Voltage (V)" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbVoltage"
                         ToolTip="Nominal voltage used when the circuit does not give one."/>
              </StackPanel>
            </Grid>
            <Border Background="#EFF6FF" CornerRadius="6" Padding="10,8">
              <TextBlock x:Name="tbSummary" TextWrapping="Wrap"
                         Foreground="#1E3A8A" FontSize="11.5"/>
            </Border>
            <TextBlock Style="{StaticResource Hint}"
                       Text="drop (V) = mV/A/m x current x length / 1000, and the percentage is of the nominal voltage. Three-phase cable tables already allow for the phase configuration."/>
          </StackPanel>
        </Border>

        <Border Style="{StaticResource Card}">
          <StackPanel>
            <TextBlock Text="In this model" Style="{StaticResource CardTitle}"
                       Margin="0,0,0,10"/>
            <TextBlock x:Name="tbModelSummary" TextWrapping="Wrap"
                       Foreground="#334155" FontSize="11.5"/>
          </StackPanel>
        </Border>
      </StackPanel>
    </Grid>
    </ScrollViewer>

    <Border Grid.Row="2" Background="White" BorderBrush="#E2E8F0"
            BorderThickness="0,1,0,0" Padding="20,14">
      <DockPanel>
        <StackPanel DockPanel.Dock="Right" Orientation="Horizontal"
                    VerticalAlignment="Center">
          <Button x:Name="btnCancel" Content="Cancel"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,10,0"
                  IsCancel="True"/>
          <Button x:Name="btnRun" Content="Measure routes"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <TextBlock VerticalAlignment="Center" TextWrapping="Wrap"
                   Foreground="#64748B" FontSize="11" Margin="0,0,16,0"
                   Text="Nothing is changed in the model while measuring. The preview opens when the routes are ready, and you choose there whether to draw or export."/>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


class SetupWindow(forms.WPFWindow):
    """Scope, cable figures and tolerance - before anything is measured."""

    def __init__(self, settings, panels, isolator_count, tray_count):
        forms.WPFWindow.__init__(self, SETUP_XAML, literal_string=True)
        self.panels = panels
        self.panel_ids = None           # None = every panel
        self.values = None
        self.ready = False
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass
        self.cbPairs.IsChecked = settings["scope_pairs"] == "1"
        self.cbCircuits.IsChecked = settings["scope_circuits"] == "1"
        self.cbIsolators.IsChecked = settings["scope_isolators"] == "1"
        self.cbDraw.IsChecked = settings["draw_lines"] == "1"
        self.tbAllowance.Text = settings["allowance"]
        self.tbTolerance.Text = settings["tolerance"]
        self.tbVoltage.Text = settings["voltage"]
        self.tbCurrent.Text = settings["current"]
        self.tbMvAm.Text = settings["mv_per_a_m"]
        self.tbLimit.Text = settings["limit_percent"]
        self.tbMaxPair.Text = settings["max_pair_metres"]
        self.tbModelSummary.Text = (
            "{} electrical equipment item(s) that can act as a panel.\n"
            "{} isolator(s) found by Comments, JAC-EL-Rating or family "
            "name.\n{} cable tray(s) to build the network from.".format(
                len(panels), isolator_count, tray_count
            )
        )
        self.btnChoosePanels.Click += self.on_choose_panels
        self.btnRun.Click += self.on_run
        self.btnCancel.Click += self.on_cancel
        for box in (self.tbAllowance, self.tbVoltage, self.tbCurrent,
                    self.tbMvAm, self.tbLimit, self.tbMaxPair):
            box.TextChanged += self.on_changed
        for check in (self.cbPairs, self.cbCircuits, self.cbIsolators):
            check.Checked += self.on_changed
            check.Unchecked += self.on_changed
        self.ready = True
        self.update_scope_text()
        self.update_summary()

    # ----- live text ---------------------------------------------------------
    def update_scope_text(self):
        if self.panel_ids is None:
            self.tbPanelScope.Text = "All {} panel(s)".format(len(self.panels))
            return
        names = [get_name(panel) for panel in self.panels
                 if element_id_value(panel.Id) in self.panel_ids]
        shown = ", ".join(sorted(names)[:6])
        if len(names) > 6:
            shown += " and {} more".format(len(names) - 6)
        self.tbPanelScope.Text = "{} of {} panel(s): {}".format(
            len(names), len(self.panels), shown
        )

    def on_changed(self, sender, args):
        self.update_summary()

    def update_summary(self):
        if not self.ready:
            return
        count = (len(self.panels) if self.panel_ids is None
                 else len(self.panel_ids))
        pairs = count * (count - 1) // 2 if self.cbPairs.IsChecked == True \
            else 0
        try:
            allowance = float(self.tbAllowance.Text.strip())
            current = float(self.tbCurrent.Text.strip())
            voltage = float(self.tbVoltage.Text.strip())
            mv_per_a_m = float(self.tbMvAm.Text.strip())
            limit = float(self.tbLimit.Text.strip())
        except ValueError:
            self.tbSummary.Text = (
                "Enter the allowance, mV/A/m, limit, current and voltage as "
                "numbers to preview the calculation."
            )
            return
        if voltage <= 0.0:
            self.tbSummary.Text = "The voltage must be more than zero."
            return
        example = 100.0 * (1.0 + allowance / 100.0)
        drop = voltage_drop_volts(example, current, mv_per_a_m)
        percent = drop / voltage * 100.0
        allowed = (voltage * limit / 100.0)
        reach = (allowed / (mv_per_a_m * current / 1000.0)
                 if mv_per_a_m * current > 0 else 0.0)
        text = (
            "{} panel pair route(s) to measure. A 100 m route becomes "
            "{:.1f} m with allowance and drops {:.2f} V, which is {:.2f} % "
            "of {:.0f} V.".format(pairs, example, drop, percent, voltage)
        )
        if reach > 0.0:
            text += (
                " At {:g} A on this cable the {:g} % limit is reached at "
                "about {:.0f} m of cable.".format(current, limit, reach)
            )
        self.tbSummary.Text = text

    # ----- buttons ------------------------------------------------------------
    def on_choose_panels(self, sender, args):
        labels = {}
        for panel in self.panels:
            labels[equipment_label(panel)] = element_id_value(panel.Id)
        chosen = forms.SelectFromList.show(
            sorted(labels),
            title="Panels to measure (search by name, mark, comments or ID)",
            button_name="Use these panels",
            multiselect=True
        )
        if chosen:
            self.panel_ids = set(labels[label] for label in chosen
                                 if label in labels)
            if not self.panel_ids:
                self.panel_ids = None
        self.update_scope_text()
        self.update_summary()

    @staticmethod
    def read_number(text, label, minimum=0.0, allow_zero=True):
        try:
            value = float(str(text).strip())
        except ValueError:
            raise ValueError("{} must be a number.".format(label))
        if math.isnan(value) or math.isinf(value):
            raise ValueError("{} must be a real number.".format(label))
        if value < minimum or (not allow_zero and abs(value) < EPSILON):
            raise ValueError("{} must be more than {:g}.".format(
                label, minimum))
        return value

    def on_run(self, sender, args):
        try:
            if (self.cbPairs.IsChecked != True
                    and self.cbCircuits.IsChecked != True
                    and self.cbIsolators.IsChecked != True):
                raise ValueError(
                    "Choose at least one thing to measure: panel pairs, "
                    "circuit equipment or isolators."
                )
            values = {
                "scope_pairs": self.cbPairs.IsChecked == True,
                "scope_circuits": self.cbCircuits.IsChecked == True,
                "scope_isolators": self.cbIsolators.IsChecked == True,
                "draw_lines": self.cbDraw.IsChecked == True,
                "panel_ids": self.panel_ids,
                "allowance": self.read_number(
                    self.tbAllowance.Text, "Cable allowance"),
                "tolerance": self.read_number(
                    self.tbTolerance.Text, "Tray connection tolerance",
                    minimum=1.0, allow_zero=False),
                "voltage": self.read_number(
                    self.tbVoltage.Text, "Voltage", minimum=1.0,
                    allow_zero=False),
                "current": self.read_number(
                    self.tbCurrent.Text, "Current", minimum=0.0),
                "mv_per_a_m": self.read_number(
                    self.tbMvAm.Text, "Cable mV/A/m", minimum=0.0),
                "limit_percent": self.read_number(
                    self.tbLimit.Text, "Voltage drop limit", minimum=0.0),
                "max_pair_metres": self.read_number(
                    self.tbMaxPair.Text, "Pair length limit", minimum=0.0),
            }
            self.values = values
            self.Close()
        except ValueError as error:
            forms.alert(str(error), title=TITLE, warn_icon=True)

    def on_cancel(self, sender, args):
        self.values = None
        self.Close()


def values_to_settings(values):
    return {
        "scope_pairs": "1" if values["scope_pairs"] else "0",
        "scope_circuits": "1" if values["scope_circuits"] else "0",
        "scope_isolators": "1" if values["scope_isolators"] else "0",
        "draw_lines": "1" if values["draw_lines"] else "0",
        "allowance": "{:g}".format(values["allowance"]),
        "tolerance": "{:g}".format(values["tolerance"]),
        "voltage": "{:g}".format(values["voltage"]),
        "current": "{:g}".format(values["current"]),
        "mv_per_a_m": "{:g}".format(values["mv_per_a_m"]),
        "limit_percent": "{:g}".format(values["limit_percent"]),
        "max_pair_metres": "{:g}".format(values["max_pair_metres"]),
    }

# =============================================================================
# RESULTS PANEL (WPF, tabbed, with the 3D preview)
# =============================================================================
MAX_TABLE_ROWS = 300        # rows drawn at once; the filter narrows the rest

RESULT_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Panel Cable Routes" Width="1120" SizeToContent="Height"
        ResizeMode="NoResize" WindowStartupLocation="CenterScreen"
        ShowInTaskbar="False" Background="#F1F4F9"
        FontFamily="Segoe UI" FontSize="12" Foreground="#1E293B"
        UseLayoutRounding="True">
  <Window.Resources>
    <Style x:Key="Card" TargetType="Border">
      <Setter Property="Background" Value="White"/>
      <Setter Property="BorderBrush" Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="10"/>
      <Setter Property="Padding" Value="14,12"/>
    </Style>
    <Style x:Key="Hint" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="Label" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#334155"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Height" Value="28"/>
      <Setter Property="Padding" Value="8,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="BorderBrush" Value="#CBD5E1"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Height" Value="28"/>
      <Setter Property="Padding" Value="8,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="FontSize" Value="11.5"/>
    </Style>
    <Style TargetType="TabControl">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Padding" Value="0,10,0,0"/>
    </Style>
    <Style TargetType="TabItem">
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Foreground" Value="#475569"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TabItem">
            <Border x:Name="bd" Background="Transparent" CornerRadius="8"
                    Padding="14,8" Margin="0,0,6,0">
              <ContentPresenter ContentSource="Header"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E2E8F0"/>
              </Trigger>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="bd" Property="Background" Value="White"/>
                <Setter Property="Foreground" Value="#1D4ED8"/>
                <Setter Property="FontWeight" Value="SemiBold"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="PrimaryButton" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="Background" Value="#2563EB"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Height" Value="36"/>
      <Setter Property="Padding" Value="18,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#1D4ED8"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="#94A3B8"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SecondaryButton" TargetType="Button">
      <Setter Property="Foreground" Value="#334155"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Height" Value="36"/>
      <Setter Property="Padding" Value="14,0"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="#CBD5E1" BorderThickness="1"
                    CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#F1F5F9"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="SmallButton" TargetType="Button"
           BasedOn="{StaticResource SecondaryButton}">
      <Setter Property="Height" Value="25"/>
      <Setter Property="Padding" Value="9,0"/>
      <Setter Property="FontSize" Value="10.5"/>
      <Setter Property="FontWeight" Value="Normal"/>
      <Setter Property="Margin" Value="4,0,0,0"/>
    </Style>
    <Style x:Key="RowCard" TargetType="Border">
      <Setter Property="Background" Value="White"/>
      <Setter Property="BorderBrush" Value="#E2E8F0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="8"/>
      <Setter Property="Padding" Value="10,7"/>
      <Setter Property="Margin" Value="0,0,0,5"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>
    <Style x:Key="RowCardBad" TargetType="Border"
           BasedOn="{StaticResource RowCard}">
      <Setter Property="Background" Value="#FEF2F2"/>
      <Setter Property="BorderBrush" Value="#FCA5A5"/>
    </Style>
    <Style x:Key="RowCardGood" TargetType="Border"
           BasedOn="{StaticResource RowCard}">
      <Setter Property="Background" Value="#F0FDF4"/>
      <Setter Property="BorderBrush" Value="#BBF7D0"/>
    </Style>
    <Style x:Key="RowCardPicked" TargetType="Border"
           BasedOn="{StaticResource RowCard}">
      <Setter Property="Background" Value="#FEFCE8"/>
      <Setter Property="BorderBrush" Value="#FACC15"/>
      <Setter Property="BorderThickness" Value="2"/>
    </Style>
    <Style x:Key="RowTitle" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#0F172A"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
    </Style>
    <Style x:Key="RowMeta" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
      <Setter Property="Margin" Value="0,2,0,0"/>
    </Style>
    <Style x:Key="RowNumber" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#1E293B"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="FontSize" Value="11.5"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="HorizontalAlignment" Value="Right"/>
    </Style>
    <Style x:Key="RowBad" TargetType="TextBlock"
           BasedOn="{StaticResource RowNumber}">
      <Setter Property="Foreground" Value="#B91C1C"/>
    </Style>
    <Style x:Key="RowGood" TargetType="TextBlock"
           BasedOn="{StaticResource RowNumber}">
      <Setter Property="Foreground" Value="#166534"/>
    </Style>
    <Style x:Key="BannerGood" TargetType="Border">
      <Setter Property="Background" Value="#F0FDF4"/>
      <Setter Property="BorderBrush" Value="#BBF7D0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="6"/>
      <Setter Property="Padding" Value="10,7"/>
    </Style>
    <Style x:Key="BannerBad" TargetType="Border">
      <Setter Property="Background" Value="#FEF2F2"/>
      <Setter Property="BorderBrush" Value="#FCA5A5"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="6"/>
      <Setter Property="Padding" Value="10,7"/>
    </Style>
    <Style x:Key="BannerText" TargetType="TextBlock">
      <Setter Property="FontSize" Value="11.5"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="Swatch" TargetType="Border">
      <Setter Property="Width" Value="11"/>
      <Setter Property="Height" Value="11"/>
      <Setter Property="CornerRadius" Value="3"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="Margin" Value="0,0,4,0"/>
    </Style>
    <Style x:Key="SwatchText" TargetType="TextBlock">
      <Setter Property="FontSize" Value="10.5"/>
      <Setter Property="Foreground" Value="#475569"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="Margin" Value="0,0,11,0"/>
    </Style>
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <!-- ===== Header ===== -->
    <Border Grid.Row="0" Padding="24,16">
      <Border.Background>
        <LinearGradientBrush StartPoint="0,0" EndPoint="1,0">
          <GradientStop Color="#1E3A8A" Offset="0"/>
          <GradientStop Color="#2563EB" Offset="1"/>
        </LinearGradientBrush>
      </Border.Background>
      <DockPanel>
        <Border DockPanel.Dock="Left" Width="44" Height="44" CornerRadius="10"
                Background="#33FFFFFF" Margin="0,0,14,0">
          <Path Stroke="White" StrokeThickness="2.4" StrokeLineJoin="Round"
                StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Data="M 3,21 L 10,21 L 14,5 L 18,21 L 25,21"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="Panel cable routes" FontSize="19"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock x:Name="tbHeadline" FontSize="12" Foreground="#DBEAFE"
                     Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <!-- ===== Tabs ===== -->
    <TabControl Grid.Row="1" Margin="20,14,20,0" BorderThickness="0">

      <!-- 3D preview -->
      <TabItem x:Name="tab3d">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <Grid Margin="0,0,0,8">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <WrapPanel Grid.Column="0" VerticalAlignment="Center"
                         Margin="0,0,10,0">
                <Border Style="{StaticResource Swatch}" Background="#94A3B8"
                        Opacity="0.55"/>
                <TextBlock Text="cable tray"
                           Style="{StaticResource SwatchText}"/>
                <Border Style="{StaticResource Swatch}" Background="#2563EB"/>
                <TextBlock Text="panel / equipment"
                           Style="{StaticResource SwatchText}"/>
                <Border Style="{StaticResource Swatch}" Background="#22D3EE"/>
                <TextBlock Text="route" Style="{StaticResource SwatchText}"/>
                <Border Style="{StaticResource Swatch}" Background="#EF4444"/>
                <TextBlock Text="over the limit"
                           Style="{StaticResource SwatchText}"/>
                <Border Style="{StaticResource Swatch}" Background="#FACC15"/>
                <TextBlock Text="picked route"
                           Style="{StaticResource SwatchText}"/>
              </WrapPanel>
              <StackPanel Grid.Column="1" Orientation="Horizontal">
                <Button x:Name="btnViewIso" Content="Iso"
                        Style="{StaticResource SmallButton}"/>
                <Button x:Name="btnViewTop" Content="Top"
                        Style="{StaticResource SmallButton}"/>
                <Button x:Name="btnViewFront" Content="Front"
                        Style="{StaticResource SmallButton}"/>
                <Button x:Name="btnViewSide" Content="Side"
                        Style="{StaticResource SmallButton}"/>
                <Button x:Name="btnViewFit" Content="Fit"
                        Style="{StaticResource SmallButton}"/>
              </StackPanel>
            </Grid>
            <Border x:Name="brd3d" Background="#0F172A" CornerRadius="8"
                    Height="420" ClipToBounds="True">
              <Viewport3D x:Name="vp3d" ClipToBounds="True"/>
            </Border>
            <Grid Margin="0,8,0,0">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBlock x:Name="tb3dStatus" Grid.Column="0"
                         Style="{StaticResource Hint}"
                         VerticalAlignment="Center" Margin="0,0,14,0"/>
              <CheckBox x:Name="cbShowTrays" Grid.Column="1" IsChecked="True"
                        VerticalAlignment="Center" Margin="0,0,12,0"
                        VerticalContentAlignment="Center"
                        ToolTip="Draw the modelled cable tray network the routes follow.">
                <TextBlock Text="Trays" Style="{StaticResource Label}"/>
              </CheckBox>
              <CheckBox x:Name="cbShowPanels" Grid.Column="2" IsChecked="True"
                        VerticalAlignment="Center" Margin="0,0,12,0"
                        VerticalContentAlignment="Center"
                        ToolTip="Draw a box for every panel and every piece of equipment a route reaches.">
                <TextBlock Text="Panels" Style="{StaticResource Label}"/>
              </CheckBox>
              <CheckBox x:Name="cbShowAllRoutes" Grid.Column="3"
                        IsChecked="True" VerticalAlignment="Center"
                        VerticalContentAlignment="Center"
                        ToolTip="Draw every route in the table. Untick to show only the route picked in the Routes tab.">
                <TextBlock Text="All routes" Style="{StaticResource Label}"/>
              </CheckBox>
            </Grid>
          </StackPanel>
        </Border>
      </TabItem>

      <!-- Routes -->
      <TabItem x:Name="tabRoutes">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <Grid Grid.Row="0" Margin="0,0,0,8">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBox x:Name="tbFilter" Grid.Column="0" Margin="0,0,10,0"
                       ToolTip="Type part of a panel name, a circuit number, a level or an ID to narrow the list."/>
              <CheckBox x:Name="cbOnlyOver" Grid.Column="1"
                        VerticalAlignment="Center" Margin="0,0,10,0"
                        VerticalContentAlignment="Center"
                        ToolTip="Show only the routes whose voltage drop is over the limit.">
                <TextBlock Text="Over the limit only"
                           Style="{StaticResource Label}"/>
              </CheckBox>
              <ComboBox x:Name="cbSort" Grid.Column="2" Width="190"
                        Margin="0,0,10,0">
                <ComboBoxItem Content="Sort: worst voltage drop" Tag="drop"/>
                <ComboBoxItem Content="Sort: longest route" Tag="length"/>
                <ComboBoxItem Content="Sort: source panel" Tag="source"/>
                <ComboBoxItem Content="Sort: destination" Tag="destination"/>
                <ComboBoxItem Content="Sort: connection type" Tag="kind"/>
              </ComboBox>
              <StackPanel Grid.Column="3" Orientation="Horizontal">
                <Button x:Name="btnTickAll" Content="Tick all"
                        Style="{StaticResource SmallButton}"/>
                <Button x:Name="btnTickNone" Content="Tick none"
                        Style="{StaticResource SmallButton}"/>
              </StackPanel>
            </Grid>
            <TextBlock x:Name="tbRouteStatus" Grid.Row="1"
                       Style="{StaticResource Hint}" Margin="0,0,0,8"/>
            <ScrollViewer Grid.Row="2" MaxHeight="420"
                          VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spRoutes"/>
            </ScrollViewer>
          </Grid>
        </Border>
      </TabItem>

      <!-- Panels -->
      <TabItem x:Name="tabPanels">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <TextBlock Style="{StaticResource Hint}" Margin="0,0,0,8"
                       Text="One row per panel in scope: how many routes touch it, its longest route and its worst voltage drop."/>
            <ScrollViewer MaxHeight="470" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spPanels"/>
            </ScrollViewer>
          </StackPanel>
        </Border>
      </TabItem>

      <!-- Overview and issues -->
      <TabItem x:Name="tabIssues">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="1.1*"/>
              <ColumnDefinition Width="16"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <ScrollViewer Grid.Column="0" MaxHeight="470"
                          VerticalScrollBarVisibility="Auto">
              <TextBlock x:Name="tbOverview" TextWrapping="Wrap"
                         FontFamily="Consolas" FontSize="11.5"/>
            </ScrollViewer>
            <StackPanel Grid.Column="2">
              <TextBlock Style="{StaticResource Hint}" Margin="0,0,0,8"
                         Text="Equipment that could not be reached, and routes that had to bridge a gap in the modelled trays."/>
              <ScrollViewer MaxHeight="440" VerticalScrollBarVisibility="Auto">
                <StackPanel x:Name="spIssues"/>
              </ScrollViewer>
            </StackPanel>
          </Grid>
        </Border>
      </TabItem>
    </TabControl>

    <!-- ===== Footer ===== -->
    <Border Grid.Row="2" Background="White" BorderBrush="#E2E8F0"
            BorderThickness="0,1,0,0" Padding="20,12" Margin="0,14,0,0">
      <DockPanel>
        <StackPanel DockPanel.Dock="Right" Orientation="Horizontal"
                    VerticalAlignment="Center">
          <Button x:Name="btnCopy" Content="Copy report"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnCsv" Content="Export CSV"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnExcel" Content="Open in Excel"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnClose" Content="Close"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,10,0"
                  IsCancel="True"/>
          <Button x:Name="btnDraw" Content="Draw detail lines"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <Border x:Name="brdBanner" Style="{StaticResource BannerGood}"
                VerticalAlignment="Center" Margin="0,0,16,0">
          <TextBlock x:Name="tbBanner" Style="{StaticResource BannerText}"/>
        </Border>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


SORT_KEYS = {
    "drop": lambda route: (-route["drop_percent"], -route["total"]),
    "length": lambda route: -route["with_allowance"],
    "source": lambda route: (route["source_name"].lower(),
                             route["destination_name"].lower()),
    "destination": lambda route: (route["destination_name"].lower(),
                                  route["source_name"].lower()),
    "kind": lambda route: (route["kind"], -route["drop_percent"]),
}


class ResultWindow(forms.WPFWindow):
    """The measured routes: 3D preview, table, panel summary and issues."""

    def __init__(self, result, values, network):
        forms.WPFWindow.__init__(self, RESULT_XAML, literal_string=True)
        self.result = result
        self.values = values
        self.network = network
        self.routes = result["routes"]
        self.panel_summary = panel_rows(self.routes, result["panels"])
        self.shown = []
        self.picked = None
        self.choice = "close"
        self.exported = []
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass
        # 3D view state
        self.azimuth, self.elevation = PRESET_VIEWS["iso"]
        self.distance = 24.0
        self.target = [0.0, 0.0, 0.0]
        self.drag_mode = None
        self.drag_origin = None
        self.camera = PerspectiveCamera()
        self.camera.FieldOfView = 45.0
        self.vp3d.Camera = self.camera
        self.brd3d.MouseLeftButtonDown += self.on_view_down
        self.brd3d.MouseRightButtonDown += self.on_view_down
        self.brd3d.MouseLeftButtonUp += self.on_view_up
        self.brd3d.MouseRightButtonUp += self.on_view_up
        self.brd3d.MouseMove += self.on_view_move
        self.brd3d.MouseWheel += self.on_view_wheel
        for button, preset in ((self.btnViewIso, "iso"),
                               (self.btnViewTop, "top"),
                               (self.btnViewFront, "front"),
                               (self.btnViewSide, "side")):
            button.Click += self.preset_handler(preset)
        self.btnViewFit.Click += self.on_view_fit
        for check in (self.cbShowTrays, self.cbShowPanels,
                      self.cbShowAllRoutes):
            check.Checked += self.on_view_option
            check.Unchecked += self.on_view_option
        # Table
        self.cbSort.SelectedIndex = 0
        self.cbSort.SelectionChanged += self.on_table_changed
        self.tbFilter.TextChanged += self.on_table_changed
        self.cbOnlyOver.Checked += self.on_table_changed
        self.cbOnlyOver.Unchecked += self.on_table_changed
        self.btnTickAll.Click += self.tick_handler(True)
        self.btnTickNone.Click += self.tick_handler(False)
        # Footer
        self.btnCopy.Click += self.on_copy
        self.btnCsv.Click += self.on_csv
        self.btnExcel.Click += self.on_excel
        self.btnDraw.Click += self.on_draw
        self.btnClose.Click += self.on_close
        self.btnDraw.IsEnabled = bool(self.routes)
        self.refresh()

    # ----- filling ------------------------------------------------------------
    def refresh(self):
        over = [route for route in self.routes if not route["within_limit"]]
        self.tab3d.Header = "3D preview"
        self.tabRoutes.Header = "Routes ({})".format(len(self.routes))
        self.tabPanels.Header = "Panels ({})".format(
            len([row for row in self.panel_summary if row["count"]])
        )
        self.tabIssues.Header = "Overview and issues ({})".format(
            len(self.result["unreachable"])
        )
        self.tbHeadline.Text = (
            "{} route(s) from {} panel(s) - {} over the {:g} % limit. "
            "Nothing is drawn in the model until you ask.".format(
                len(self.routes), len(self.result["panels"]), len(over),
                self.values["limit_percent"]
            )
        )
        self.tbOverview.Text = "\n".join(
            report_lines(self.result, self.values, self.network)
        )
        self.show_routes()
        self.show_panels()
        self.show_issues()
        self.update_banner()
        self.build_scene()

    def update_banner(self, message=None, good=None):
        over = [route for route in self.routes if not route["within_limit"]]
        ticked = len(self.ticked_routes())
        if message is None:
            if not self.routes:
                message = "No routes were measured."
                good = False
            elif over:
                message = (
                    "{} of {} route(s) are over the {:g} % limit. {} route(s) "
                    "ticked for drawing and export.".format(
                        len(over), len(self.routes),
                        self.values["limit_percent"], ticked
                    )
                )
                good = False
            else:
                message = (
                    "Every route is within the {:g} % limit. {} route(s) "
                    "ticked for drawing and export.".format(
                        self.values["limit_percent"], ticked
                    )
                )
                good = True
        self.brdBanner.Style = self.FindResource(
            "BannerGood" if good else "BannerBad"
        )
        self.tbBanner.Foreground = media_brush(
            (255, 22, 101, 52) if good else (255, 185, 28, 28)
        )
        self.tbBanner.Text = message

    def current_sort(self):
        item = self.cbSort.SelectedItem
        return str(item.Tag) if item is not None else "drop"

    def filtered_routes(self):
        text = (self.tbFilter.Text or "").strip().lower()
        only_over = self.cbOnlyOver.IsChecked == True
        found = []
        for route in self.routes:
            if only_over and route["within_limit"]:
                continue
            if text:
                haystack = u" ".join((
                    route["source_name"], route["destination_name"],
                    route["source_level"], route["destination_level"],
                    route["circuit_number"], route["load_name"],
                    ROUTE_KIND_NAMES.get(route["kind"], ""),
                    str(route["source_id"]), str(route["destination_id"]),
                )).lower()
                if text not in haystack:
                    continue
            found.append(route)
        found.sort(key=SORT_KEYS.get(self.current_sort(), SORT_KEYS["drop"]))
        return found

    def ticked_routes(self):
        return [route for route in self.routes if route["include"]]

    def show_routes(self):
        self.spRoutes.Children.Clear()
        self.shown = self.filtered_routes()
        ticked = len(self.ticked_routes())
        note = "Showing {} of {} route(s); {} ticked.".format(
            min(len(self.shown), MAX_TABLE_ROWS), len(self.routes), ticked
        )
        if len(self.shown) > MAX_TABLE_ROWS:
            note += (" Only the first {} are listed - narrow the filter, or "
                     "use Export for the whole set.".format(MAX_TABLE_ROWS))
        note += "  Click a row to pick it out in the 3D preview."
        self.tbRouteStatus.Text = note
        if not self.shown:
            self.spRoutes.Children.Add(self.message_row(
                "No route matches the filter.", "RowCard", "RowNumber"
            ))
            return
        for route in self.shown[:MAX_TABLE_ROWS]:
            self.spRoutes.Children.Add(self.make_route_row(route))

    def make_route_row(self, route):
        card = Border()
        if route is self.picked:
            card.Style = self.FindResource("RowCardPicked")
        elif route["within_limit"]:
            card.Style = self.FindResource("RowCard")
        else:
            card.Style = self.FindResource("RowCardBad")
        grid = Grid()
        for spec in (GridLength(24), GridLength(1, GridUnitType.Star),
                     GridLength(86), GridLength(72), GridLength(58),
                     GridLength(108)):
            column = ColumnDefinition()
            column.Width = spec
            grid.ColumnDefinitions.Add(column)

        tick = CheckBox()
        tick.IsChecked = route["include"]
        tick.VerticalAlignment = VerticalAlignment.Center
        tick.ToolTip = "Include this route when drawing and exporting"
        tick.Click += self.tick_route_handler(route, tick)

        details = StackPanel()
        title = TextBlock()
        title.Style = self.FindResource("RowTitle")
        title.Text = route_title(route)
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        pieces = [ROUTE_KIND_NAMES.get(route["kind"], route["kind"])]
        if route["circuit_number"]:
            pieces.append(u"circuit {}".format(route["circuit_number"]))
        if route["load_name"]:
            pieces.append(route["load_name"])
        levels = u" / ".join(
            item for item in (route["source_level"],
                              route["destination_level"]) if item
        )
        if levels:
            pieces.append(levels)
        pieces.append(u"{:.1f} A {}".format(
            route["current"],
            "from the circuit" if route["current_from_circuit"]
            else "(dialog)"
        ))
        pieces.append(u"tray {:.1f} m + ends {:.1f} m".format(
            metres(route["tray_length"]),
            metres(route["source_gap"] + route["destination_gap"])
        ))
        if route["bridge"] > EPSILON:
            pieces.append(u"bridged gap {:.1f} m".format(
                metres(route["bridge"])
            ))
        meta.Text = u"  |  ".join(pieces)
        details.Children.Add(title)
        details.Children.Add(meta)

        length = TextBlock()
        length.Style = self.FindResource("RowNumber")
        length.Text = "{:.2f} m".format(metres(route["with_allowance"]))
        length.ToolTip = "Route length including the cable allowance"

        drop = TextBlock()
        drop.Style = self.FindResource(
            "RowGood" if route["within_limit"] else "RowBad"
        )
        drop.Text = "{:.2f} %".format(route["drop_percent"])
        drop.ToolTip = "{:.2f} V on {:.0f} V".format(
            route["drop_volts"], route["voltage"]
        )

        status = TextBlock()
        status.Style = self.FindResource(
            "RowGood" if route["within_limit"] else "RowBad"
        )
        status.Text = "OK" if route["within_limit"] else "OVER"

        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        select_button = Button()
        select_button.Content = "Select"
        select_button.Style = self.FindResource("SmallButton")
        select_button.ToolTip = "Select both ends in Revit"
        select_button.Click += self.focus_handler(route, False)
        zoom_button = Button()
        zoom_button.Content = "Zoom"
        zoom_button.Style = self.FindResource("SmallButton")
        zoom_button.ToolTip = "Zoom the active view to this route"
        zoom_button.Click += self.focus_handler(route, True)
        buttons.Children.Add(select_button)
        buttons.Children.Add(zoom_button)

        for column, element in enumerate(
                (tick, details, length, drop, status, buttons)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        card.MouseLeftButtonUp += self.pick_handler(route)
        return card

    def show_panels(self):
        self.spPanels.Children.Clear()
        if not self.panel_summary:
            self.spPanels.Children.Add(self.message_row(
                "No panels in scope.", "RowCard", "RowNumber"
            ))
            return
        for row in self.panel_summary:
            self.spPanels.Children.Add(self.make_panel_row(row))

    def make_panel_row(self, row):
        card = Border()
        over = row["over_limit"] > 0
        card.Style = self.FindResource("RowCardBad" if over else "RowCard")
        grid = Grid()
        for spec in (GridLength(1, GridUnitType.Star), GridLength(86),
                     GridLength(92), GridLength(72), GridLength(56)):
            column = ColumnDefinition()
            column.Width = spec
            grid.ColumnDefinitions.Add(column)

        details = StackPanel()
        title = TextBlock()
        title.Style = self.FindResource("RowTitle")
        title.Text = row["name"]
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        pieces = []
        if row["level"]:
            pieces.append(row["level"])
        pieces.append("ID {}".format(element_id_value(row["panel"].Id)))
        if row["over_limit"]:
            pieces.append("{} route(s) over the limit".format(
                row["over_limit"]))
        if row["bridged"]:
            pieces.append("{} route(s) bridge a tray gap".format(
                row["bridged"]))
        meta.Text = "  |  ".join(pieces)
        details.Children.Add(title)
        details.Children.Add(meta)

        count = TextBlock()
        count.Style = self.FindResource("RowNumber")
        count.Text = "{} route(s)".format(row["count"])
        longest = TextBlock()
        longest.Style = self.FindResource("RowNumber")
        longest.Text = ("{:.2f} m".format(metres(row["longest"]))
                        if row["count"] else "-")
        longest.ToolTip = "Longest route touching this panel"
        worst = TextBlock()
        worst.Style = self.FindResource("RowBad" if over else "RowGood")
        worst.Text = ("{:.2f} %".format(row["worst_percent"])
                      if row["count"] else "-")
        worst.ToolTip = "Worst voltage drop touching this panel"

        button = Button()
        button.Content = "Select"
        button.Style = self.FindResource("SmallButton")
        button.Click += self.element_focus_handler(row["panel"])

        for column, element in enumerate(
                (details, count, longest, worst, button)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        return card

    def show_issues(self):
        self.spIssues.Children.Clear()
        unreachable = self.result["unreachable"]
        bridged = [route for route in self.routes
                   if route["bridge"] > EPSILON]
        if not unreachable and not bridged:
            self.spIssues.Children.Add(self.message_row(
                "Every item in scope was reached through the modelled "
                "trays.", "RowCardGood", "RowGood"
            ))
            return
        for element, reason in unreachable[:MAX_TABLE_ROWS]:
            card = Border()
            card.Style = self.FindResource("RowCardBad")
            grid = Grid()
            for spec in (GridLength(1, GridUnitType.Star), GridLength(56)):
                column = ColumnDefinition()
                column.Width = spec
                grid.ColumnDefinitions.Add(column)
            details = StackPanel()
            title = TextBlock()
            title.Style = self.FindResource("RowTitle")
            title.Text = get_name(element)
            meta = TextBlock()
            meta.Style = self.FindResource("RowMeta")
            meta.Text = u"{}  |  ID {}".format(
                reason, element_id_value(element.Id)
            )
            details.Children.Add(title)
            details.Children.Add(meta)
            button = Button()
            button.Content = "Select"
            button.Style = self.FindResource("SmallButton")
            button.Click += self.element_focus_handler(element)
            for column, item in enumerate((details, button)):
                Grid.SetColumn(item, column)
                grid.Children.Add(item)
            card.Child = grid
            self.spIssues.Children.Add(card)
        for route in bridged[:MAX_TABLE_ROWS]:
            self.spIssues.Children.Add(self.message_row(
                u"{}: the modelled trays do not join up - {:.2f} m of the "
                u"route is a straight bridge between tray ends.".format(
                    route_title(route), metres(route["bridge"])
                ), "RowCardBad", "RowBad"
            ))

    def message_row(self, message, card_style, text_style):
        card = Border()
        card.Style = self.FindResource(card_style)
        label = TextBlock()
        label.Style = self.FindResource(text_style)
        label.Text = message
        label.TextWrapping = TextWrapping.Wrap
        label.HorizontalAlignment = HorizontalAlignment.Left
        card.Child = label
        return card

    # ----- picking and ticking ------------------------------------------------
    def tick_route_handler(self, route, checkbox):
        def handler(sender, args):
            route["include"] = checkbox.IsChecked == True
            self.update_banner()
            try:
                args.Handled = True        # do not also pick the row
            except Exception:
                pass
        return handler

    def tick_handler(self, state):
        def handler(sender, args):
            for route in (self.shown or self.routes):
                route["include"] = state
            self.show_routes()
            self.update_banner()
        return handler

    def pick_handler(self, route):
        def handler(sender, args):
            self.picked = None if route is self.picked else route
            self.show_routes()
            self.build_scene()
            if self.picked is None:
                self.update_banner()
            else:
                self.update_banner(
                    u"Picked {}: {:.2f} m, {:.2f} V drop ({:.2f} %). It is "
                    u"highlighted in the 3D preview.".format(
                        route_title(route),
                        metres(route["with_allowance"]),
                        route["drop_volts"], route["drop_percent"]
                    ),
                    route["within_limit"]
                )
        return handler

    def focus_handler(self, route, zoom):
        def handler(sender, args):
            self.focus_elements(
                [route["source"], route["destination"]], zoom,
                route_title(route)
            )
            try:
                args.Handled = True
            except Exception:
                pass
        return handler

    def element_focus_handler(self, element):
        def handler(sender, args):
            self.focus_elements([element], True, get_name(element))
            try:
                args.Handled = True
            except Exception:
                pass
        return handler

    def focus_elements(self, elements, zoom, label):
        """Select, and optionally zoom to, these elements in Revit."""
        try:
            element_ids = List[ElementId]()
            for element in elements:
                element_ids.Add(element.Id)
            uidoc.Selection.SetElementIds(element_ids)
            if zoom:
                uidoc.ShowElements(element_ids)
            self.update_banner(
                u"{} selected in Revit - move or close this window to look "
                u"at it.".format(label), True
            )
        except Exception as error:
            self.update_banner(
                u"Revit could not show {}: {}".format(label, error), False
            )

    def on_table_changed(self, sender, args):
        self.show_routes()
        self.build_scene()

    def on_view_option(self, sender, args):
        self.build_scene()

    # ----- 3D preview ---------------------------------------------------------
    def build_scene(self):
        """Draw the tray network, the panels and the routes."""
        try:
            self.vp3d.Children.Clear()
            meshes = {}
            for key in ("tray", "panel", "route", "over", "selected"):
                meshes[key] = MeshGeometry3D()
            cloud = []          # what the camera frames: routes and panels
            tray_cloud = []
            boxes = 0
            show_all = self.cbShowAllRoutes.IsChecked == True
            if show_all:
                drawn_routes = self.shown or self.routes
            else:
                drawn_routes = [self.picked] if self.picked is not None else []
            for route in drawn_routes:
                if route is None:
                    continue
                if route is self.picked:
                    key = "selected"
                    half = feet(SELECTED_TUBE_MM) / 2.0
                elif route["within_limit"]:
                    key = "route"
                    half = feet(ROUTE_TUBE_MM) / 2.0
                else:
                    key = "over"
                    half = feet(ROUTE_TUBE_MM) / 2.0
                boxes += add_polyline(
                    meshes[key], route["points"], half, cloud,
                    max(MAX_SCENE_BOXES - boxes, 0)
                )
                if boxes >= MAX_SCENE_BOXES:
                    break
            panel_count = 0
            if self.cbShowPanels.IsChecked == True:
                seen = set()
                for route in (drawn_routes or self.routes):
                    if route is None:
                        continue
                    for element in (route["source"], route["destination"]):
                        key = element_id_value(element.Id)
                        if key in seen:
                            continue
                        seen.add(key)
                        bounds = element_bounds(element)
                        if bounds is None:
                            continue
                        corners = axis_box_corners(bounds[0], bounds[1])
                        add_box(meshes["panel"], corners)
                        cloud.extend(corners)
                        panel_count += 1
            tray_count = 0
            if self.cbShowTrays.IsChecked == True:
                for data in self.network.trays.values():
                    if boxes >= MAX_SCENE_BOXES:
                        break
                    corners = tray_prism(data)
                    if corners is None:
                        continue
                    add_box(meshes["tray"], corners)
                    tray_cloud.extend(corners)
                    tray_count += 1
                    boxes += 1

            group = Model3DGroup()
            group.Children.Add(AmbientLight(
                MediaColor.FromArgb(255, 112, 112, 120)
            ))
            group.Children.Add(DirectionalLight(
                MediaColor.FromArgb(255, 215, 215, 220),
                Vector3D(-1.0, -1.4, -2.2)
            ))
            group.Children.Add(DirectionalLight(
                MediaColor.FromArgb(255, 120, 120, 128),
                Vector3D(1.6, 1.0, 0.6)
            ))
            for key in ("tray", "panel", "route", "over", "selected"):
                if meshes[key].Positions.Count:
                    group.Children.Add(
                        shaded_model(meshes[key], VIEW_COLOURS[key])
                    )
            self.scene_transform(group, cloud or tray_cloud)
            visual = ModelVisual3D()
            visual.Content = group
            self.vp3d.Children.Add(visual)
            self.update_camera()
            self.tb3dStatus.Text = (
                "{} route(s), {} panel box(es), {} tray(s) drawn{}  |  drag "
                "to orbit, right-drag to pan, wheel to zoom.".format(
                    len([route for route in drawn_routes
                         if route is not None]),
                    panel_count, tray_count,
                    ", capped at {} boxes".format(MAX_SCENE_BOXES)
                    if boxes >= MAX_SCENE_BOXES else ""
                )
            )
        except Exception as error:
            self.tb3dStatus.Text = (
                "The 3D preview could not be drawn ({}). The other tabs are "
                "unaffected.".format(error)
            )

    def scene_transform(self, group, cloud):
        """Move the drawing to the origin and scale it to about 10 units."""
        if not cloud:
            return
        lows = [min(corner[axis] for corner in cloud) for axis in range(3)]
        highs = [max(corner[axis] for corner in cloud) for axis in range(3)]
        centre = [(lows[axis] + highs[axis]) / 2.0 for axis in range(3)]
        extent = max([highs[axis] - lows[axis] for axis in range(3)]
                     + [EPSILON])
        scale = 10.0 / extent
        transform = Transform3DGroup()
        transform.Children.Add(TranslateTransform3D(
            -centre[0], -centre[1], -centre[2]
        ))
        transform.Children.Add(ScaleTransform3D(scale, scale, scale))
        group.Transform = transform

    def update_camera(self):
        azimuth = math.radians(self.azimuth)
        elevation = math.radians(max(-89.0, min(89.0, self.elevation)))
        offset = (
            self.distance * math.cos(elevation) * math.cos(azimuth),
            self.distance * math.cos(elevation) * math.sin(azimuth),
            self.distance * math.sin(elevation),
        )
        self.camera.Position = Point3D(self.target[0] + offset[0],
                                       self.target[1] + offset[1],
                                       self.target[2] + offset[2])
        self.camera.LookDirection = Vector3D(-offset[0], -offset[1],
                                             -offset[2])
        self.camera.UpDirection = Vector3D(0.0, 0.0, 1.0)

    def preset_handler(self, preset):
        def handler(sender, args):
            self.azimuth, self.elevation = PRESET_VIEWS[preset]
            self.update_camera()
        return handler

    def on_view_fit(self, sender, args):
        self.distance = 24.0
        self.target = [0.0, 0.0, 0.0]
        self.update_camera()

    def on_view_down(self, sender, args):
        self.drag_mode = (
            "pan" if args.ChangedButton.ToString() == "Right" else "orbit"
        )
        self.drag_origin = args.GetPosition(self.brd3d)
        self.brd3d.CaptureMouse()

    def on_view_up(self, sender, args):
        self.drag_mode = None
        self.brd3d.ReleaseMouseCapture()

    def on_view_move(self, sender, args):
        if self.drag_mode is None or self.drag_origin is None:
            return
        position = args.GetPosition(self.brd3d)
        delta_x = position.X - self.drag_origin.X
        delta_y = position.Y - self.drag_origin.Y
        self.drag_origin = position
        if self.drag_mode == "orbit":
            self.azimuth = (self.azimuth - delta_x * 0.4) % 360.0
            self.elevation = max(-89.0,
                                 min(89.0, self.elevation + delta_y * 0.4))
        else:
            azimuth = math.radians(self.azimuth)
            elevation = math.radians(self.elevation)
            right = (-math.sin(azimuth), math.cos(azimuth), 0.0)
            forward = (-math.cos(elevation) * math.cos(azimuth),
                       -math.cos(elevation) * math.sin(azimuth),
                       -math.sin(elevation))
            up = vector_cross(right, forward)
            step = self.distance * 0.0016
            for axis in range(3):
                self.target[axis] -= right[axis] * delta_x * step
                self.target[axis] += up[axis] * delta_y * step
        self.update_camera()

    def on_view_wheel(self, sender, args):
        factor = 0.88 if args.Delta > 0 else 1.0 / 0.88
        self.distance = max(1.5, min(240.0, self.distance * factor))
        self.update_camera()

    # ----- export and close ---------------------------------------------------
    def clipboard_text(self):
        lines = report_lines(self.result, self.values, self.network)
        lines.append("")
        lines.append(u"\t".join(EXPORT_HEADER))
        for row in export_rows(self.ticked_routes(), self.values):
            lines.append(u"\t".join(u"{}".format(item) for item in row))
        return u"\n".join(lines)

    def on_copy(self, sender, args):
        try:
            Clipboard.SetText(self.clipboard_text())
        except Exception as error:
            self.update_banner(
                "Could not copy to the clipboard: {}".format(error), False
            )
            return
        self.update_banner(
            "Report and {} ticked route(s) copied - paste straight into "
            "Excel, the columns are tab separated.".format(
                len(self.ticked_routes())
            ), True
        )

    def export_file(self):
        """Write the ticked routes to a CSV. Returns the path, or None."""
        routes = self.ticked_routes()
        if not routes:
            self.update_banner(
                "Tick at least one route to export.", False
            )
            return None
        path = ask_export_path()
        if not path:
            return None
        try:
            write_csv(path, EXPORT_HEADER, export_rows(routes, self.values))
        except Exception as error:
            self.update_banner(
                "Could not write the file: {}".format(error), False
            )
            return None
        self.exported.append(path)
        return path

    def on_csv(self, sender, args):
        path = self.export_file()
        if path:
            self.update_banner(
                u"{} route(s) exported to {}".format(
                    len(self.ticked_routes()), path
                ), True
            )

    def on_excel(self, sender, args):
        path = self.export_file()
        if not path:
            return
        try:
            saved = open_in_excel(path)
        except Exception as error:
            self.update_banner(
                u"Excel could not be opened ({}). The file is written and "
                u"waiting at {}".format(error, path), False
            )
            return
        self.update_banner(
            u"Opened in Excel with the header frozen and filters on{}.".format(
                u", saved as {}".format(saved) if saved
                else u" (still a CSV - save it as a workbook in Excel)"
            ), True
        )

    def on_draw(self, sender, args):
        if not self.ticked_routes():
            self.update_banner("Tick at least one route to draw.", False)
            return
        self.choice = "draw"
        self.Close()

    def on_close(self, sender, args):
        self.choice = "close"
        self.Close()


# =============================================================================
# DRAWING THE ROUTES (the only part that changes the model)
# =============================================================================
def get_red_style():
    """The red line subcategory the routes are drawn with, made if needed."""
    lines = doc.Settings.Categories.get_Item("Lines")
    for subcategory in lines.SubCategories:
        if subcategory.Name == ROUTE_STYLE_NAME:
            return subcategory.GetGraphicsStyle(GraphicsStyleType.Projection)
    subcategory = doc.Settings.Categories.NewSubcategory(
        lines, ROUTE_STYLE_NAME)
    subcategory.LineColor = Color(255, 0, 0)
    subcategory.SetLineWeight(6, GraphicsStyleType.Projection)
    return subcategory.GetGraphicsStyle(GraphicsStyleType.Projection)


def project_to_view(point, target_view):
    """Flatten a model point onto the plane of a plan view."""
    origin = target_view.Origin
    normal = target_view.ViewDirection.Normalize()
    return point - normal.Multiply((point - origin).DotProduct(normal))


def simplify_route_points(points, minimum_length, tolerance=0.001):
    """Drop duplicates, straighten collinear runs and remove stubs."""
    cleaned = []
    for point in points:
        if not cleaned or cleaned[-1].DistanceTo(point) > tolerance:
            cleaned.append(point)

    index = 1
    while index < len(cleaned) - 1:
        first = cleaned[index] - cleaned[index - 1]
        second = cleaned[index + 1] - cleaned[index]
        if (first.DotProduct(second) >= 0.0
                and first.CrossProduct(second).GetLength()
                <= tolerance * (first.GetLength() + second.GetLength())):
            del cleaned[index]
        else:
            index += 1

    index = 0
    while index < len(cleaned) - 1:
        if cleaned[index].DistanceTo(cleaned[index + 1]) >= minimum_length:
            index += 1
            continue
        if index == 0:
            del cleaned[1]
        elif index + 1 == len(cleaned) - 1:
            del cleaned[index]
            index = max(index - 1, 0)
        else:
            del cleaned[index + 1]
    return cleaned


def previous_route_lines(line_style, target_view):
    """Detail lines a previous run drew in this view, to be replaced."""
    found = []
    for detail in FilteredElementCollector(
            doc, target_view.Id).OfClass(CurveElement):
        try:
            if (detail.ViewSpecific
                    and detail.OwnerViewId == target_view.Id
                    and detail.LineStyle
                    and detail.LineStyle.Id == line_style.Id):
                found.append(detail.Id)
        except Exception:
            continue
    return found


def get_route_views(source_element, destination_element):
    """The active plan, plus a plan view for each end's level."""
    route_views = {element_id_value(view.Id): view}
    wanted_levels = set()
    for element in (source_element, destination_element):
        level_id = get_element_level_id(element)
        if level_id is not None:
            wanted_levels.add(element_id_value(level_id))
    if not wanted_levels:
        return list(route_views.values()), []

    matching = []
    for plan_view in FilteredElementCollector(doc).OfClass(ViewPlan):
        try:
            if (plan_view.IsTemplate
                    or plan_view.ViewType != view.ViewType
                    or plan_view.GenLevel is None
                    or element_id_value(plan_view.GenLevel.Id)
                    not in wanted_levels):
                continue
            matching.append(plan_view)
        except Exception:
            continue

    found_levels = set()
    try:
        active_level = view.GenLevel
        if (active_level is not None
                and element_id_value(active_level.Id) in wanted_levels):
            found_levels.add(element_id_value(active_level.Id))
    except Exception:
        pass
    for candidate in sorted(matching, key=lambda item: item.Name):
        level_id = element_id_value(candidate.GenLevel.Id)
        if level_id in found_levels:
            continue
        route_views[element_id_value(candidate.Id)] = candidate
        found_levels.add(level_id)
    return list(route_views.values()), sorted(wanted_levels - found_levels)


def draw_routes(routes):
    """Draw the routes as red detail lines, replacing a previous run's.

    Everything happens in one transaction: on any failure nothing is left
    behind, and one Undo removes the lot."""
    summary = {
        "created": 0, "deleted": 0, "views": 0, "errors": [],
        "missing_levels": [], "committed": False,
    }
    transaction = Transaction(doc, "Draw panel cable routes")
    transaction.Start()
    try:
        red_style = get_red_style()
        views_by_route = {}
        drawing_views = {element_id_value(view.Id): view}
        missing_levels = set()
        for route in routes:
            route_views, missing = get_route_views(
                route["source"], route["destination"]
            )
            views_by_route[id(route)] = route_views
            for target_view in route_views:
                drawing_views[element_id_value(target_view.Id)] = target_view
            missing_levels.update(missing)
        summary["views"] = len(drawing_views)
        summary["missing_levels"] = sorted(missing_levels)

        stale = []
        for target_view in drawing_views.values():
            stale.extend(previous_route_lines(red_style, target_view))

        short_curve = doc.Application.ShortCurveTolerance
        for route in routes:
            for target_view in views_by_route[id(route)]:
                cleaned = simplify_route_points(
                    [project_to_view(point, target_view)
                     for point in route["points"]],
                    short_curve
                )
                drawn = 0
                for index in range(len(cleaned) - 1):
                    if (cleaned[index].DistanceTo(cleaned[index + 1])
                            < short_curve):
                        continue
                    try:
                        detail = doc.Create.NewDetailCurve(
                            target_view,
                            Line.CreateBound(cleaned[index],
                                             cleaned[index + 1])
                        )
                        detail.LineStyle = red_style
                        summary["created"] += 1
                        drawn += 1
                    except Exception as error:
                        if len(summary["errors"]) < 10:
                            summary["errors"].append(u"{} in {}: {}".format(
                                route_title(route), target_view.Name, error
                            ))
                if drawn == 0 and len(summary["errors"]) < 10:
                    summary["errors"].append(
                        u"{} in {}: nothing was left to draw after "
                        u"flattening onto the view.".format(
                            route_title(route), target_view.Name
                        )
                    )

        if summary["created"] == 0:
            raise Exception(
                summary["errors"][0] if summary["errors"]
                else "Revit did not accept any of the route segments."
            )
        for element_id in stale:
            try:
                doc.Delete(element_id)
                summary["deleted"] += 1
            except Exception:
                pass
        transaction.Commit()
        summary["committed"] = True
    except Exception as error:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        summary["failure"] = "{}".format(error)
    return summary


# =============================================================================
# MAIN
# =============================================================================
# ---- 1. The view has to be a plan, because the routes are drawn as detail
#         lines in plan views ------------------------------------------------
if view.ViewType.ToString() not in PLAN_VIEW_TYPES:
    forms.alert(
        "Open the plan view you want the routes drawn in before running "
        "this tool (floor plan, ceiling plan or engineering plan).",
        title=TITLE, exitscript=True
    )

# ---- 2. What is in the model ------------------------------------------------
panels = collect_panels()
if not panels:
    forms.alert(
        "No electrical equipment was found, so there are no panels to "
        "measure between.",
        title=TITLE, exitscript=True
    )
isolators_by_id = collect_isolators()
tray_count = (FilteredElementCollector(doc)
              .OfCategory(BuiltInCategory.OST_CableTray)
              .WhereElementIsNotElementType()
              .GetElementCount())
if not tray_count:
    forms.alert(
        "No cable trays were found in this model. The routes follow the "
        "modelled tray centre lines, so there is nothing to measure along.",
        title=TITLE, exitscript=True
    )

# ---- 3. Dialog ---------------------------------------------------------------
stored_settings, user_config = load_settings()
setup = SetupWindow(stored_settings, panels, len(isolators_by_id), tray_count)
setup.ShowDialog()
values = setup.values
if values is None:
    script.exit()
save_settings(user_config, values_to_settings(values))

# ---- 4. The tray network -----------------------------------------------------
network = TrayNetwork(values["tolerance"])
if not network.ready:
    forms.alert(
        "The cable trays in this model gave no usable centre lines.",
        title=TITLE, exitscript=True
    )

scope_ids = values["panel_ids"]
if scope_ids is None:
    scope_ids = set(element_id_value(panel.Id) for panel in panels)
if len(scope_ids) < 2 and values["scope_pairs"]:
    forms.alert(
        "Panel-to-panel needs at least two panels in scope. Choose more "
        "panels, or measure the circuit equipment instead.",
        title=TITLE, warn_icon=True
    )

# ---- 5. Measure --------------------------------------------------------------
result = build_routes(network, values, panels, isolators_by_id, scope_ids)
if not result["routes"]:
    forms.alert(
        "No routes could be measured. Check that the panels sit near the "
        "modelled cable trays, and that the trays are connected (the "
        "connection tolerance is {:g} mm).".format(values["tolerance"]),
        title=TITLE, exitscript=True
    )

# ---- 6. Preview, then draw / export -----------------------------------------
window = ResultWindow(result, values, network)
window.tab3d.IsSelected = True          # open on the 3D preview
window.ShowDialog()

draw_summary = None
if window.choice == "draw":
    to_draw = [route for route in result["routes"] if route["include"]]
    draw_summary = draw_routes(to_draw)
    if not draw_summary.get("committed"):
        forms.alert(
            "The routes were measured, but the red detail lines could not "
            "be drawn - nothing was changed:\n\n{}".format(
                draw_summary.get("failure", "unknown reason")
            ),
            title=TITLE, warn_icon=True
        )

# ---- 7. Report in the pyRevit output window ---------------------------------
routes = result["routes"]
over_limit = [route for route in routes if not route["within_limit"]]
output.print_md("# Panel cable routes")
for line in report_lines(result, values, network):
    if line:
        output.print_md("- {}".format(line.strip()))

output.print_md("## Routes")
output.print_md(
    "| Source | Destination | Type | Circuit | Tray (m) | Ends (m) | "
    "Gap (m) | Length (m) | +{:g}% (m) | A | V | Drop (V) | Drop (%) | "
    "Status |".format(values["allowance"])
)
output.print_md(
    "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"
)
for route in routes:
    try:
        source_link = output.linkify(
            ElementId(route["source_id"]), title=route["source_name"])
        destination_link = output.linkify(
            ElementId(route["destination_id"]),
            title=route["destination_name"])
    except Exception:
        source_link = route["source_name"]
        destination_link = route["destination_name"]
    output.print_md(
        "| {} | {} | {} | {} | {:.2f} | {:.2f} | {:.2f} | {:.2f} | {:.2f} | "
        "{:.1f} | {:.0f} | {:.2f} | {:.2f} | {} |".format(
            source_link, destination_link,
            ROUTE_KIND_NAMES.get(route["kind"], route["kind"]),
            route["circuit_number"] or "-",
            metres(route["tray_length"]),
            metres(route["source_gap"] + route["destination_gap"]),
            metres(route["bridge"]),
            metres(route["total"]),
            metres(route["with_allowance"]),
            route["current"], route["voltage"],
            route["drop_volts"], route["drop_percent"],
            "OK" if route["within_limit"] else "**OVER**"
        )
    )

if result["unreachable"]:
    output.print_md("## Not reached")
    for element, reason in result["unreachable"]:
        try:
            link = output.linkify(element.Id, title=get_name(element))
        except Exception:
            link = get_name(element)
        output.print_md("- {} - {}".format(link, reason))

if window.exported:
    output.print_md("## Exported")
    for path in window.exported:
        output.print_md("- `{}`".format(path))

# ---- 8. Closing message ------------------------------------------------------
message = [
    "{} route(s) measured from {} panel(s).".format(
        len(routes), len(result["panels"])
    ),
    "Worst voltage drop: {:.2f} % ({} over the {:g} % limit).".format(
        max([route["drop_percent"] for route in routes] or [0.0]),
        len(over_limit), values["limit_percent"]
    ),
]
if result["cancelled"]:
    message.append(
        "The run was cancelled part way, so this is a partial set."
    )
if draw_summary is not None and draw_summary.get("committed"):
    message.append("")
    message.append(
        "{} detail line segment(s) drawn across {} view(s); {} line(s) from "
        "a previous run were replaced.".format(
            draw_summary["created"], draw_summary["views"],
            draw_summary["deleted"]
        )
    )
    if draw_summary["errors"]:
        message.append("{} segment(s) could not be drawn. First: {}".format(
            len(draw_summary["errors"]), draw_summary["errors"][0]
        ))
    if draw_summary["missing_levels"]:
        message.append(
            "No plan view was found for level id(s) {} - those routes were "
            "drawn only in the views that do exist.".format(
                ", ".join(str(item)
                          for item in draw_summary["missing_levels"])
            )
        )
if window.exported:
    message.append("")
    message.append("Exported to:")
    message.extend("  {}".format(path) for path in window.exported)
message.append("")
message.append("The full table is in the pyRevit output window.")

forms.alert("\n".join(message), title=TITLE)
