# -*- coding: utf-8 -*-
__title__ = "Circuit Room Names"
__doc__ = (
    "Write the rooms a circuit serves into a parameter on the circuit. "
    "Every device on the circuit is looked up in the rooms of this model "
    "and of the linked architectural models, and the room names are joined "
    "into one value - 'LL MCCS ROOM, CORRIDOR, ENTRANCE FACILITY ROOM'."
)

# =============================================================================
# CIRCUIT ROOM NAMES
# -----------------------------------------------------------------------------
# What it does
#   1. Finds the electrical circuits you ask for: every circuit, or only the
#      ones belonging to the panels you have selected.
#   2. For each circuit, takes the devices on it - the sockets, the outlets,
#      the equipment - and works out which room each one stands in. Rooms
#      usually live in a linked architectural model, so the links are
#      searched too, and several sample points are tried per device because
#      a socket sits inside its wall at its own location point.
#   3. Joins the room names the circuit passes through into one value, in
#      the order and format you choose, leaving out repeats.
#   4. Shows every circuit with the rooms found and the value that would be
#      written, and lists each device and its room, before anything is
#      changed. You tick what to write.
#   5. Writes the ticked values into the circuit parameter you choose, in
#      ONE transaction, so a single Undo takes it all back.
#
# The parameter has to exist already on the Electrical Circuits category.
# This tool only fills it in; it does not create project parameters.
#
# MODIFIES THE MODEL only when you press Write, and only that one parameter
# on the circuits you ticked.
#
# Revit versions: 2023, 2024, 2025, 2026, 2027 (pyRevit, IronPython engine).
#   - Element ids are read through element_id_value(): ElementId.IntegerValue
#     was removed in Revit 2026 (ElementId.Value exists from 2024).
# =============================================================================

import math
import re

from Autodesk.Revit import DB
from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, ElementId, FilteredElementCollector,
    StorageType, Transaction, XYZ
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
    SystemParameters, TextWrapping, Thickness, VerticalAlignment, Visibility
)
from System.Windows.Controls import (
    Border, Button, CheckBox, ColumnDefinition, Grid, Orientation,
    StackPanel, TextBlock
)
from System.Windows.Media import Color as MediaColor, SolidColorBrush


def media_brush(red, green, blue):
    """A solid brush, for the colours set from code."""
    return SolidColorBrush(MediaColor.FromRgb(red, green, blue))


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

TITLE = "Circuit Room Names"
MM_TO_FT = 1.0 / 304.8
EPSILON = 1e-9

# How far off a wall a hosted device is pushed when looking for its room.
WALL_OFFSET_MM = 300.0
# How far above its level a high-level item is sampled from.
LEVEL_SAMPLE_MM = 1000.0

# The circuit kinds offered in the dialog.
SYSTEM_KINDS = (
    ("power", "Power circuits only"),
    ("all", "Every kind of circuit"),
)

NAME_FORMATS = (
    ("name", "Room name", u"LL MCCS ROOM"),
    ("number_name", "Number and name", u"B02-15 LL MCCS ROOM"),
    ("number", "Room number", u"B02-15"),
)

ROOM_ORDERS = (
    ("number", "By room number"),
    ("name", "By room name"),
    ("circuit", "In the order the devices are on the circuit"),
)

SEPARATORS = (
    (", ", u"comma and space        LL MCCS ROOM, CORRIDOR"),
    (" / ", u"slash                  LL MCCS ROOM / CORRIDOR"),
    ("; ", u"semicolon              LL MCCS ROOM; CORRIDOR"),
    (" + ", u"plus                   LL MCCS ROOM + CORRIDOR"),
)

DEFAULT_SETTINGS = {
    "parameter": "",
    "scope": "all",             # all / selection
    "system_kind": "power",     # power / all
    "name_format": "name",      # name / number_name / number
    "separator": ", ",
    "order": "number",          # number / name / circuit
    "include_panel_room": "0",
    "show_counts": "0",
    "overwrite": "1",
    "use_links": "1",
    "use_spaces": "1",
    "no_room_text": "",         # written for a device with no room; blank omits
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
        if stored is not None and str(stored) != "":
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
# Ids, names and parameters
# =============================================================================
def element_id_value(element_id):
    """The number behind an ElementId, on every supported Revit version."""
    if element_id is None:
        return None
    try:
        return element_id.Value          # Revit 2024 and later
    except AttributeError:
        return element_id.IntegerValue   # up to Revit 2025


def to_element_id(value):
    try:
        return ElementId(value)
    except Exception:
        return ElementId(int(value))


def parameter_text(parameter):
    if parameter is None:
        return ""
    try:
        value = parameter.AsString()
        if value:
            return value
        value = parameter.AsValueString()
        return value if value else ""
    except Exception:
        return ""


def element_name(element):
    """Mark, then type name, then whatever Revit calls it."""
    for builtin in (BuiltInParameter.ALL_MODEL_MARK,
                    BuiltInParameter.ALL_MODEL_TYPE_MARK):
        try:
            value = parameter_text(element.get_Parameter(builtin))
            if value:
                return value
        except Exception:
            pass
    try:
        if element.Name:
            return element.Name
    except Exception:
        pass
    return "Element {}".format(element_id_value(element.Id))


def type_name(element):
    try:
        symbol = doc.GetElement(element.GetTypeId())
        if symbol is not None and symbol.Name:
            return symbol.Name
    except Exception:
        pass
    return ""


def family_and_type(element):
    try:
        symbol = doc.GetElement(element.GetTypeId())
        if symbol is None:
            return type_name(element)
        family = getattr(symbol, "Family", None)
        if family is not None and family.Name:
            return u"{} : {}".format(family.Name, symbol.Name)
        return symbol.Name
    except Exception:
        return type_name(element)


def level_name(element):
    for builtin in (BuiltInParameter.FAMILY_LEVEL_PARAM,
                    BuiltInParameter.SCHEDULE_LEVEL_PARAM):
        try:
            parameter = element.get_Parameter(builtin)
            if parameter is None:
                continue
            level = doc.GetElement(parameter.AsElementId())
            if level is not None:
                return level.Name
        except Exception:
            pass
    try:
        level = doc.GetElement(element.LevelId)
        if level is not None:
            return level.Name
    except Exception:
        pass
    return ""


def level_elevation(element):
    """The height of the element's level, for the fallback sample point."""
    for builtin in (BuiltInParameter.FAMILY_LEVEL_PARAM,
                    BuiltInParameter.SCHEDULE_LEVEL_PARAM):
        try:
            parameter = element.get_Parameter(builtin)
            if parameter is None:
                continue
            level = doc.GetElement(parameter.AsElementId())
            if level is not None:
                try:
                    return level.ProjectElevation
                except AttributeError:
                    return level.Elevation
        except Exception:
            pass
    try:
        level = doc.GetElement(element.LevelId)
        if level is not None:
            try:
                return level.ProjectElevation
            except AttributeError:
                return level.Elevation
    except Exception:
        pass
    return None

def is_category(element, builtin_category):
    """True when the element belongs to that Revit category."""
    try:
        if element.Category is None:
            return False
        return element_id_value(element.Category.Id) == int(builtin_category)
    except Exception:
        return False


# =============================================================================
# Finding the room an item stands in
# =============================================================================
# A device on a wall sits inside the wall at its own location point, and a
# unit at high level can sit above the room's upper limit, so one point is
# not enough. Several are tried, nearest-the-truth first, and the preview
# says which one found the room.
# =============================================================================
def sample_points(element):
    """Points to look for a room at, best first, each with a label."""
    points = []
    seen = []

    def add(point, label):
        if point is None:
            return
        for other in seen:
            if other.DistanceTo(point) < 1e-6:
                return
        seen.append(point)
        points.append((point, label))

    location_point = None
    try:
        location = element.Location
        if isinstance(location, DB.LocationPoint):
            location_point = location.Point
    except Exception:
        location_point = None
    add(location_point, "its own point")

    # A hosted device is inside its wall; step off the face, both ways.
    try:
        facing = element.FacingOrientation
        if location_point is not None and facing is not None:
            offset = facing.Normalize().Multiply(WALL_OFFSET_MM * MM_TO_FT)
            add(location_point + offset, "stepped off the wall")
            add(location_point - offset, "stepped off the wall, the other way")
    except Exception:
        pass

    box = None
    try:
        box = element.get_BoundingBox(None)
    except Exception:
        box = None
    if box is not None:
        add(XYZ((box.Min.X + box.Max.X) / 2.0,
                (box.Min.Y + box.Max.Y) / 2.0,
                (box.Min.Z + box.Max.Z) / 2.0), "the centre of the item")

    # High level equipment can sit above the room: drop to just above floor.
    elevation = level_elevation(element)
    if elevation is not None:
        base = location_point
        if base is None and box is not None:
            base = XYZ((box.Min.X + box.Max.X) / 2.0,
                       (box.Min.Y + box.Max.Y) / 2.0, 0.0)
        if base is not None:
            add(XYZ(base.X, base.Y, elevation + LEVEL_SAMPLE_MM * MM_TO_FT),
                "a metre above its level")
    return points


def room_record(room, kind, source):
    """Name, number and the text the rules are matched against."""
    if room is None:
        return None
    number = ""
    name = ""
    try:
        number = parameter_text(
            room.get_Parameter(BuiltInParameter.ROOM_NUMBER))
    except Exception:
        number = ""
    try:
        name = parameter_text(room.get_Parameter(BuiltInParameter.ROOM_NAME))
    except Exception:
        name = ""
    if not name:
        try:
            name = room.Name or ""
        except Exception:
            name = ""
    label = u" ".join(part for part in (number, name) if part).strip()
    return {
        "name": name,
        "number": number,
        "kind": kind,
        "source": source,
        "label": label if label else u"(unnamed {})".format(kind.lower()),
        "haystack": label,
        "id": element_id_value(room.Id),
    }


class RoomFinder(object):
    """Rooms and spaces, in this model and in the loaded links."""

    def __init__(self, use_links, use_spaces, phase):
        self.use_spaces = use_spaces
        self.phase = phase
        self.links = []
        self.link_errors = []
        if not use_links:
            return
        try:
            instances = (FilteredElementCollector(doc)
                         .OfClass(DB.RevitLinkInstance)
                         .ToElements())
        except Exception:
            instances = []
        for instance in instances:
            try:
                link_doc = instance.GetLinkDocument()
            except Exception:
                link_doc = None
            if link_doc is None:
                continue        # the link is not loaded
            try:
                name = instance.Name
            except Exception:
                name = "linked model"
            try:
                transform = instance.GetTotalTransform().Inverse
            except Exception:
                self.link_errors.append(name)
                continue
            self.links.append((name, link_doc, transform))

    @property
    def link_count(self):
        return len(self.links)

    def _room_at(self, source_doc, point, phase):
        try:
            if phase is not None:
                return source_doc.GetRoomAtPoint(point, phase)
        except Exception:
            pass
        try:
            return source_doc.GetRoomAtPoint(point)
        except Exception:
            return None

    def _space_at(self, source_doc, point, phase):
        try:
            if phase is not None:
                return source_doc.GetSpaceAtPoint(point, phase)
        except Exception:
            pass
        try:
            return source_doc.GetSpaceAtPoint(point)
        except Exception:
            return None

    def at_point(self, point):
        """The room or space at this point: this model first, then links."""
        room = self._room_at(doc, point, self.phase)
        if room is not None:
            return room_record(room, "Room", "")
        if self.use_spaces:
            space = self._space_at(doc, point, self.phase)
            if space is not None:
                return room_record(space, "Space", "")
        for name, link_doc, transform in self.links:
            try:
                local = transform.OfPoint(point)
            except Exception:
                continue
            room = self._room_at(link_doc, local, None)
            if room is not None:
                return room_record(room, "Room", name)
            if self.use_spaces:
                space = self._space_at(link_doc, local, None)
                if space is not None:
                    return room_record(space, "Space", name)
        return None

    def find(self, element):
        """The room for an element, with how it was found."""
        for point, label in sample_points(element):
            found = self.at_point(point)
            if found is not None:
                found["how"] = label
                return found
        return None


def active_phase():
    """The phase to read rooms in: the view's, else the element's, else none."""
    try:
        parameter = doc.ActiveView.get_Parameter(BuiltInParameter.VIEW_PHASE)
        if parameter is not None:
            phase = doc.GetElement(parameter.AsElementId())
            if phase is not None:
                return phase
    except Exception:
        pass
    try:
        phases = doc.Phases
        if phases is not None and phases.Size > 0:
            return phases.get_Item(phases.Size - 1)
    except Exception:
        pass
    return None


# =============================================================================
# The parameter to write
# =============================================================================
def writable_text_parameters(element, scope):
    """Names of the text parameters that can be written on this element."""
    found = []
    holder = element
    if scope == "type":
        try:
            holder = doc.GetElement(element.GetTypeId())
        except Exception:
            holder = None
    if holder is None:
        return found
    try:
        parameters = list(holder.Parameters)
    except Exception:
        return found
    for parameter in parameters:
        try:
            if parameter.IsReadOnly:
                continue
            if parameter.StorageType != StorageType.String:
                continue
            name = parameter.Definition.Name
        except Exception:
            continue
        if name:
            found.append(name)
    return found


def parameter_choices(elements, scope, limit=400):
    """Text parameters available across the elements, most common first."""
    counts = {}
    for index, element in enumerate(elements):
        if index >= limit:
            break
        for name in writable_text_parameters(element, scope):
            counts[name] = counts.get(name, 0) + 1
    choices = [(name, count) for name, count in counts.items()]
    choices.sort(key=lambda item: (-item[1], item[0].lower()))
    return choices


def parameter_holder(element, scope):
    if scope != "type":
        return element
    try:
        return doc.GetElement(element.GetTypeId())
    except Exception:
        return None


def find_parameter(element, name, scope):
    holder = parameter_holder(element, scope)
    if holder is None:
        return None
    try:
        parameter = holder.LookupParameter(name)
    except Exception:
        parameter = None
    if parameter is None:
        return None
    try:
        if parameter.IsReadOnly or parameter.StorageType != StorageType.String:
            return None
    except Exception:
        return None
    return parameter




# =============================================================================
# The circuits and the rooms they pass through
# =============================================================================
def circuit_number(system):
    try:
        return system.CircuitNumber or ""
    except Exception:
        return ""


def circuit_panel(system):
    try:
        return system.PanelName or ""
    except Exception:
        return ""


def circuit_load_name(system):
    try:
        return system.LoadName or ""
    except Exception:
        return ""


def circuit_label(system):
    """'R5 / PDB-B2-B02-01', the way the tags read on the drawing."""
    number = circuit_number(system)
    panel = circuit_panel(system)
    if number and panel:
        return u"{} / {}".format(number, panel)
    return number or panel or u"Circuit {}".format(
        element_id_value(system.Id))


def is_power_circuit(system):
    try:
        return str(system.SystemType) == "PowerCircuit"
    except Exception:
        return True         # if the kind cannot be read, do not hide it


def circuit_devices(system):
    """The elements the circuit serves."""
    try:
        return list(system.Elements)
    except Exception:
        return []


def collect_circuits(kind, panel_ids):
    """Every circuit, or only those fed by the given panels."""
    circuits = []
    try:
        found = (FilteredElementCollector(doc)
                 .OfClass(ElectricalSystem)
                 .WhereElementIsNotElementType()
                 .ToElements())
    except Exception:
        return circuits
    for element in found:
        system = element
        if kind == "power" and not is_power_circuit(system):
            continue
        if panel_ids:
            try:
                base = system.BaseEquipment
            except Exception:
                base = None
            if base is None or element_id_value(base.Id) not in panel_ids:
                continue
        circuits.append(system)
    circuits.sort(key=lambda item: (circuit_panel(item).lower(),
                                    circuit_number(item)))
    return circuits


def room_display(room, name_format):
    """How one room is written in the joined value."""
    if room is None:
        return u""
    number = room.get("number") or u""
    name = room.get("name") or u""
    if name_format == "number":
        return number or name
    if name_format == "number_name":
        return u" ".join(part for part in (number, name) if part)
    return name or number


def natural_key(text):
    """Sort 'B2-9' before 'B2-10', which a plain string sort does not."""
    parts = re.split(r"(\d+)", text or u"")
    key = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part), u""))
        elif part:
            key.append((0, 0, part.lower()))
    return key


def gather_rooms(devices, finder, values, panel_room=None):
    """The distinct rooms a set of devices sits in, in the chosen order.

    Returns the room entries and the per-device detail, so the preview can
    show both the joined value and where each device was found."""
    name_format = values["name_format"]
    entries = {}
    detail = []
    missing = 0
    order_index = 0

    def remember(room, text, sort_first=False):
        entry = entries.get(text)
        if entry is None:
            entry = {
                "text": text,
                "number": room.get("number") if room else u"",
                "name": room.get("name") if room else u"",
                "count": 0,
                "order": -1 if sort_first else order_index,
                "room": room,
            }
            entries[text] = entry
        entry["count"] += 1
        return entry

    if panel_room is not None:
        text = room_display(panel_room, name_format)
        if text:
            remember(panel_room, text, True)

    for device in devices:
        room = finder.find(device)
        text = room_display(room, name_format) if room else u""
        detail.append({
            "device": device,
            "id": element_id_value(device.Id),
            "name": element_name(device),
            "type_name": family_and_type(device),
            "room": room,
            "text": text,
        })
        if room is None or not text:
            missing += 1
            continue
        remember(room, text)
        order_index += 1

    if values["no_room_text"] and missing:
        placeholder = values["no_room_text"]
        entry = entries.get(placeholder)
        if entry is None:
            entries[placeholder] = {
                "text": placeholder, "number": u"", "name": placeholder,
                "count": missing, "order": order_index, "room": None,
                "last": True,
            }
        else:
            entry["count"] += missing

    order = values["order"]

    def sort_key(entry):
        # The panel's room leads and the no-room note trails, whichever way
        # the rooms themselves are sorted. Sorting the note by its text
        # would file "(no room)" under punctuation, at the front.
        if entry["order"] < 0:
            rank = 0
        elif entry.get("last"):
            rank = 2
        else:
            rank = 1
        if order == "circuit":
            return (rank, [(1, entry["order"], u"")])
        if order == "name":
            return (rank, natural_key(entry["name"] or entry["text"]))
        return (rank, natural_key(entry["number"] or entry["text"]))

    ordered = list(entries.values())
    ordered.sort(key=sort_key)
    return ordered, detail, missing


def joined_value(entries, values):
    """The text that goes in the parameter."""
    separator = values["separator"]
    pieces = []
    for entry in entries:
        text = entry["text"]
        if values["show_counts"] and entry["count"] > 1:
            text = u"{} ({})".format(text, entry["count"])
        pieces.append(text)
    return separator.join(pieces)


# -----------------------------------------------------------------------------
# What would change
# -----------------------------------------------------------------------------
STATUS_WRITE = "write"
STATUS_SAME = "same"
STATUS_KEEP = "keep"
STATUS_NO_ROOMS = "no_rooms"
STATUS_NO_DEVICES = "no_devices"
STATUS_NO_PARAM = "no_param"

STATUS_TEXT = {
    STATUS_WRITE: "will be written",
    STATUS_SAME: "already correct",
    STATUS_KEEP: "has a different value, kept",
    STATUS_NO_ROOMS: "no room found for any device",
    STATUS_NO_DEVICES: "nothing is connected to this circuit",
    STATUS_NO_PARAM: "the parameter is not on this circuit",
}


def build_changes(circuits, finder, values, progress=None):
    """One record per circuit: the rooms, the value, and what would happen."""
    parameter_name = values["parameter"]
    overwrite = values["overwrite"]
    changes = []
    total = max(len(circuits), 1)
    for index, system in enumerate(circuits):
        if progress is not None and not progress(index + 1, total):
            break
        devices = circuit_devices(system)
        panel_room = None
        if values["include_panel_room"]:
            try:
                base = system.BaseEquipment
            except Exception:
                base = None
            if base is not None:
                panel_room = finder.find(base)
        entries, detail, missing = gather_rooms(
            devices, finder, values, panel_room)
        wanted = joined_value(entries, values)

        parameter = find_parameter(system, parameter_name, "instance")
        current = parameter_text(parameter) if parameter is not None else u""
        if parameter is None:
            status = STATUS_NO_PARAM
        elif not devices:
            status = STATUS_NO_DEVICES
        elif not entries:
            status = STATUS_NO_ROOMS
        elif current == wanted:
            status = STATUS_SAME
        elif current and not overwrite:
            status = STATUS_KEEP
        else:
            status = STATUS_WRITE

        changes.append({
            "system": system,
            "id": element_id_value(system.Id),
            "label": circuit_label(system),
            "number": circuit_number(system),
            "panel": circuit_panel(system),
            "load": circuit_load_name(system),
            "devices": detail,
            "device_count": len(devices),
            "entries": entries,
            "missing": missing,
            "current": current,
            "wanted": wanted,
            "status": status,
            "include": status == STATUS_WRITE,
        })
    return changes


# =============================================================================
# SETUP DIALOG (WPF)
# =============================================================================
SETUP_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Circuit Room Names" Width="980" SizeToContent="Height"
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
      <Setter Property="Margin" Value="0,0,0,7"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="RadioButton">
      <Setter Property="Margin" Value="0,0,14,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Padding" Value="8,4"/>
      <Setter Property="BorderBrush" Value="#CBD5E1"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Height" Value="30"/>
      <Setter Property="Padding" Value="8,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
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
      <Setter Property="Height" Value="30"/>
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
          <Path Stroke="White" StrokeThickness="2.2" StrokeLineJoin="Round"
                StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Data="M 4,6 L 4,24 M 4,10 L 16,10 L 16,20 L 26,20 M 16,15 L 26,15 M 24,18 L 28,18 L 28,22 L 24,22 Z"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="Circuit Room Names" FontSize="20"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock Text="Write the rooms a circuit serves into a parameter on the circuit"
                     FontSize="12" Foreground="#DBEAFE" Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
    <Grid Margin="20,18,20,4">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="16"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>

      <StackPanel Grid.Column="0">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,12">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="1" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Which circuits"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <ComboBox x:Name="cbKind" Margin="0,0,0,10"
                      ToolTip="Power circuits are the usual case. The other kinds - data, fire alarm, security - can be included if the parameter is on them too."/>
            <CheckBox x:Name="cbSelectionOnly" Visibility="Collapsed"
                      ToolTip="Work only on the circuits fed by the panels you had selected when you started.">
              <TextBlock x:Name="tbSelectionText" FontWeight="SemiBold"
                         Foreground="#334155"/>
            </CheckBox>
            <TextBlock x:Name="tbCircuitCount" Style="{StaticResource Hint}"/>
          </StackPanel>
        </Border>

        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,12">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="2" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Which parameter to write"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <ComboBox x:Name="cbParameter"
                      ToolTip="Only text parameters on the circuits that can be written are listed, with how many circuits have each one."/>
            <TextBlock x:Name="tbParameterHint" Style="{StaticResource Hint}"/>
            <CheckBox x:Name="cbOverwrite" Margin="0,12,0,0"
                      ToolTip="On: the rooms replace whatever is in the parameter. Off: a circuit that already has something is listed but left alone.">
              <TextBlock Text="Replace values that are already there"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <CheckBox x:Name="cbLinks" Margin="0,8,0,0"
                      ToolTip="Rooms normally live in the linked architectural model, so this is usually needed.">
              <TextBlock Text="Look for rooms in linked models"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <CheckBox x:Name="cbSpaces"
                      ToolTip="If no Room is found at a point, try an MEP Space there instead.">
              <TextBlock Text="Fall back to MEP spaces"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <TextBlock x:Name="tbLinkText" Style="{StaticResource Hint}"/>
          </StackPanel>
        </Border>
      </StackPanel>

      <StackPanel Grid.Column="2">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,12">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="3" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="How the value reads"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <Grid Margin="0,0,0,10">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <StackPanel Grid.Column="0">
                <TextBlock Text="Each room as" Style="{StaticResource Label}"/>
                <ComboBox x:Name="cbFormat"/>
              </StackPanel>
              <StackPanel Grid.Column="2">
                <TextBlock Text="Listed" Style="{StaticResource Label}"/>
                <ComboBox x:Name="cbOrder"/>
              </StackPanel>
            </Grid>
            <TextBlock Text="Joined with" Style="{StaticResource Label}"/>
            <ComboBox x:Name="cbSeparator" Margin="0,0,0,10"/>
            <CheckBox x:Name="cbCounts"
                      ToolTip="Adds how many devices are in the room: CORRIDOR (3).">
              <TextBlock Text="Show how many devices are in each room"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <CheckBox x:Name="cbPanelRoom"
                      ToolTip="Puts the room the panel itself stands in at the front of the list.">
              <TextBlock Text="Start with the panel's own room"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <TextBlock Text="For a device with no room"
                       Style="{StaticResource Label}" Margin="0,6,0,5"/>
            <TextBox x:Name="tbNoRoom"
                     ToolTip="Leave this empty to leave such devices out of the value; the preview still reports them. Put something here - (no room) - to have it appear at the end of the list."/>
            <Border Background="#EFF6FF" CornerRadius="6" Padding="10,8"
                    Margin="0,12,0,0">
              <StackPanel>
                <TextBlock Text="A circuit through three rooms would read:"
                           Foreground="#1E3A8A" FontSize="11"/>
                <TextBlock x:Name="tbExample" TextWrapping="Wrap"
                           Foreground="#1E3A8A" FontSize="12"
                           FontWeight="SemiBold" Margin="0,4,0,0"/>
              </StackPanel>
            </Border>
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
          <Button x:Name="btnCancel" Content="Cancel" IsCancel="True"
                  Style="{StaticResource SecondaryButton}" Height="36"
                  Margin="0,0,10,0"/>
          <Button x:Name="btnRun" Content="Check the circuits"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <TextBlock VerticalAlignment="Center" TextWrapping="Wrap"
                   Foreground="#64748B" FontSize="11" Margin="0,0,16,0"
                   Text="Nothing is changed yet. Every circuit is listed with the rooms found and the value it would be given, and you choose what to write."/>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""

# Rooms used for the live example in the dialog.
EXAMPLE_ROOMS = (
    {"number": u"B02-15", "name": u"LL MCCS ROOM"},
    {"number": u"B02-20", "name": u"CORRIDOR"},
    {"number": u"B02-31", "name": u"ENTRANCE FACILITY ROOM"},
)


class SetupWindow(forms.WPFWindow):
    """Which circuits, which parameter, and how the value reads."""

    def __init__(self, settings, panel_ids):
        forms.WPFWindow.__init__(self, SETUP_XAML, literal_string=True)
        self.panel_ids = panel_ids
        self.values = None
        self.ready = False
        self.parameter_names = []
        self.circuits = []
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass

        for key, label in SYSTEM_KINDS:
            self.cbKind.Items.Add(label)
        self.cbKind.SelectedIndex = 0
        for index, (key, label) in enumerate(SYSTEM_KINDS):
            if key == settings["system_kind"]:
                self.cbKind.SelectedIndex = index

        for key, label, example in NAME_FORMATS:
            self.cbFormat.Items.Add(u"{}        {}".format(label, example))
        self.cbFormat.SelectedIndex = 0
        for index, (key, _, _) in enumerate(NAME_FORMATS):
            if key == settings["name_format"]:
                self.cbFormat.SelectedIndex = index

        for key, label in ROOM_ORDERS:
            self.cbOrder.Items.Add(label)
        self.cbOrder.SelectedIndex = 0
        for index, (key, _) in enumerate(ROOM_ORDERS):
            if key == settings["order"]:
                self.cbOrder.SelectedIndex = index

        for separator, label in SEPARATORS:
            self.cbSeparator.Items.Add(label)
        self.cbSeparator.SelectedIndex = 0
        for index, (separator, _) in enumerate(SEPARATORS):
            if separator == settings["separator"]:
                self.cbSeparator.SelectedIndex = index

        if panel_ids:
            self.cbSelectionOnly.Visibility = Visibility.Visible
            self.tbSelectionText.Text = (
                "Only the circuits of the {} selected panel(s)".format(
                    len(panel_ids))
            )
            self.cbSelectionOnly.IsChecked = settings["scope"] == "selection"
            self.cbSelectionOnly.Checked += self.on_scope_changed
            self.cbSelectionOnly.Unchecked += self.on_scope_changed

        self.cbOverwrite.IsChecked = settings["overwrite"] == "1"
        self.cbLinks.IsChecked = settings["use_links"] == "1"
        self.cbSpaces.IsChecked = settings["use_spaces"] == "1"
        self.cbCounts.IsChecked = settings["show_counts"] == "1"
        self.cbPanelRoom.IsChecked = settings["include_panel_room"] == "1"
        self.tbNoRoom.Text = settings["no_room_text"]
        self.remembered_parameter = settings["parameter"]

        self.cbKind.SelectionChanged += self.on_scope_changed
        for combo in (self.cbFormat, self.cbOrder, self.cbSeparator):
            combo.SelectionChanged += self.on_format_changed
        for box in (self.cbCounts, self.cbPanelRoom):
            box.Checked += self.on_format_changed
            box.Unchecked += self.on_format_changed
        self.tbNoRoom.TextChanged += self.on_format_changed
        self.btnRun.Click += self.on_run
        self.btnCancel.Click += self.on_cancel
        self.ready = True
        self.refresh_circuits()
        self.update_example()
        self.update_links_text()

    # ----- reading the dialog -------------------------------------------------
    def current_kind(self):
        index = self.cbKind.SelectedIndex
        return SYSTEM_KINDS[index][0] if 0 <= index < len(SYSTEM_KINDS) \
            else "power"

    def current_format(self):
        index = self.cbFormat.SelectedIndex
        return NAME_FORMATS[index][0] if 0 <= index < len(NAME_FORMATS) \
            else "name"

    def current_order(self):
        index = self.cbOrder.SelectedIndex
        return ROOM_ORDERS[index][0] if 0 <= index < len(ROOM_ORDERS) \
            else "number"

    def current_separator(self):
        index = self.cbSeparator.SelectedIndex
        return SEPARATORS[index][0] if 0 <= index < len(SEPARATORS) else ", "

    def use_selection(self):
        return bool(self.panel_ids) and self.cbSelectionOnly.IsChecked == True

    def format_values(self):
        return {
            "name_format": self.current_format(),
            "order": self.current_order(),
            "separator": self.current_separator(),
            "show_counts": self.cbCounts.IsChecked == True,
            "include_panel_room": self.cbPanelRoom.IsChecked == True,
            "no_room_text": (self.tbNoRoom.Text or "").strip(),
        }

    # ----- live refresh -------------------------------------------------------
    def refresh_circuits(self):
        if not self.ready:
            return
        panel_ids = self.panel_ids if self.use_selection() else None
        self.circuits = collect_circuits(self.current_kind(), panel_ids)
        self.tbCircuitCount.Text = "{} circuit(s) found.".format(
            len(self.circuits))
        self.refresh_parameters()

    def refresh_parameters(self):
        choices = parameter_choices(self.circuits, "instance")
        self.parameter_names = [name for name, _ in choices]
        self.cbParameter.Items.Clear()
        if not choices:
            self.cbParameter.Items.Add(
                "(no writable text parameter on these circuits)")
            self.cbParameter.SelectedIndex = 0
            self.cbParameter.IsEnabled = False
            self.tbParameterHint.Text = (
                "Add a text project parameter to the Electrical Circuits "
                "category and run this again."
            )
            return
        for name, count in choices:
            self.cbParameter.Items.Add(u"{}   (on {} of {})".format(
                name, count, len(self.circuits)))
        self.cbParameter.IsEnabled = True
        chosen = 0
        for index, name in enumerate(self.parameter_names):
            if name == self.remembered_parameter:
                chosen = index
                break
            if "room" in name.lower():
                chosen = index
        self.cbParameter.SelectedIndex = chosen
        self.tbParameterHint.Text = (
            "Only text parameters on the circuits that can be written are "
            "listed."
        )

    def update_links_text(self):
        count = 0
        loaded = 0
        try:
            for instance in (FilteredElementCollector(doc)
                             .OfClass(DB.RevitLinkInstance).ToElements()):
                count += 1
                if instance.GetLinkDocument() is not None:
                    loaded += 1
        except Exception:
            pass
        self.tbLinkText.Text = (
            "No linked models in this project." if count == 0
            else "{} linked model(s), {} loaded.".format(count, loaded)
        )

    def update_example(self):
        values = self.format_values()
        entries = []
        for index, room in enumerate(EXAMPLE_ROOMS):
            text = room_display(room, values["name_format"])
            entries.append({
                "text": text, "number": room["number"], "name": room["name"],
                "count": 3 if index == 1 else 1,
                "order": index, "room": room,
            })
        if values["include_panel_room"]:
            panel_room = {"number": u"B02-10", "name": u"LL MAIN GSM ROOM"}
            entries.insert(0, {
                "text": room_display(panel_room, values["name_format"]),
                "number": panel_room["number"], "name": panel_room["name"],
                "count": 1, "order": -1, "room": panel_room,
            })
        if values["no_room_text"]:
            entries.append({
                "text": values["no_room_text"], "number": u"",
                "name": values["no_room_text"], "count": 1,
                "order": 99, "room": None, "last": True,
            })
        self.tbExample.Text = joined_value(entries, values)

    def on_scope_changed(self, sender, args):
        self.refresh_circuits()

    def on_format_changed(self, sender, args):
        if self.ready:
            self.update_example()

    # ----- buttons -------------------------------------------------------------
    def on_run(self, sender, args):
        try:
            if not self.circuits:
                raise ValueError(
                    "No circuits were found. Check the kind of circuit "
                    "above, and that the model has circuits in it.")
            if not self.parameter_names or not self.cbParameter.IsEnabled:
                raise ValueError(
                    "There is no writable text parameter on these circuits, "
                    "so there is nowhere to put the room names.")
            index = self.cbParameter.SelectedIndex
            if index < 0 or index >= len(self.parameter_names):
                raise ValueError("Choose the parameter to write.")
            values = self.format_values()
            values.update({
                "parameter": self.parameter_names[index],
                "system_kind": self.current_kind(),
                "scope": "selection" if self.use_selection() else "all",
                "circuits": self.circuits,
                "overwrite": self.cbOverwrite.IsChecked == True,
                "use_links": self.cbLinks.IsChecked == True,
                "use_spaces": self.cbSpaces.IsChecked == True,
            })
            self.values = values
            self.Close()
        except ValueError as error:
            forms.alert(str(error), title=TITLE, warn_icon=True)

    def on_cancel(self, sender, args):
        self.values = None
        self.Close()


def values_to_settings(values):
    return {
        "parameter": values["parameter"],
        "scope": values["scope"],
        "system_kind": values["system_kind"],
        "name_format": values["name_format"],
        "separator": values["separator"],
        "order": values["order"],
        "include_panel_room": "1" if values["include_panel_room"] else "0",
        "show_counts": "1" if values["show_counts"] else "0",
        "overwrite": "1" if values["overwrite"] else "0",
        "use_links": "1" if values["use_links"] else "0",
        "use_spaces": "1" if values["use_spaces"] else "0",
        "no_room_text": values["no_room_text"],
    }


# =============================================================================
# PREVIEW WINDOW (WPF) - nothing is written until you press Write
# =============================================================================
MAX_TABLE_ROWS = 400

RESULT_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Circuit Room Names" Width="1180" Height="820"
        MinWidth="960" MinHeight="640"
        WindowStartupLocation="CenterScreen" ShowInTaskbar="False"
        Background="#F1F4F9" FontFamily="Segoe UI" FontSize="12"
        Foreground="#1E293B" UseLayoutRounding="True">
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
      <Setter Property="Height" Value="34"/>
      <Setter Property="Padding" Value="16,0"/>
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
      <Setter Property="Height" Value="34"/>
      <Setter Property="Padding" Value="12,0"/>
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
    </Style>
    <Style x:Key="RowCardWrite" TargetType="Border"
           BasedOn="{StaticResource RowCard}">
      <Setter Property="Background" Value="#F0FDF4"/>
      <Setter Property="BorderBrush" Value="#BBF7D0"/>
    </Style>
    <Style x:Key="RowCardWarn" TargetType="Border"
           BasedOn="{StaticResource RowCard}">
      <Setter Property="Background" Value="#FFFBEB"/>
      <Setter Property="BorderBrush" Value="#FDE68A"/>
    </Style>
    <Style x:Key="RowCardBad" TargetType="Border"
           BasedOn="{StaticResource RowCard}">
      <Setter Property="Background" Value="#FEF2F2"/>
      <Setter Property="BorderBrush" Value="#FCA5A5"/>
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
    <Style x:Key="RowValue" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#1E293B"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="FontSize" Value="11.5"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="RowGood" TargetType="TextBlock"
           BasedOn="{StaticResource RowValue}">
      <Setter Property="Foreground" Value="#166534"/>
    </Style>
    <Style x:Key="RowWarn" TargetType="TextBlock"
           BasedOn="{StaticResource RowValue}">
      <Setter Property="Foreground" Value="#92400E"/>
    </Style>
    <Style x:Key="RowBad" TargetType="TextBlock"
           BasedOn="{StaticResource RowValue}">
      <Setter Property="Foreground" Value="#B91C1C"/>
    </Style>
  </Window.Resources>
  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <Border Grid.Row="0" Padding="24,14">
      <Border.Background>
        <LinearGradientBrush StartPoint="0,0" EndPoint="1,0">
          <GradientStop Color="#1E3A8A" Offset="0"/>
          <GradientStop Color="#2563EB" Offset="1"/>
        </LinearGradientBrush>
      </Border.Background>
      <DockPanel>
        <Border DockPanel.Dock="Left" Width="42" Height="42" CornerRadius="10"
                Background="#33FFFFFF" Margin="0,0,14,0">
          <Path Stroke="White" StrokeThickness="2.2" StrokeLineJoin="Round"
                StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Data="M 4,6 L 4,24 M 4,10 L 16,10 L 16,20 L 26,20 M 16,15 L 26,15"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="Circuit room names" FontSize="18"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock x:Name="tbHeadline" FontSize="12" Foreground="#DBEAFE"
                     Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <TabControl Grid.Row="1" Margin="18,12,18,0" BorderThickness="0">
      <TabItem x:Name="tabCircuits">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="*"/>
            </Grid.RowDefinitions>
            <Grid Grid.Row="0" Margin="0,0,0,8">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBox x:Name="tbFilter" Grid.Column="0" Margin="0,0,10,0"
                       ToolTip="Type part of a circuit number, a panel, a room or a load name to narrow the list."/>
              <ComboBox x:Name="cbStatusFilter" Grid.Column="1" Width="260"
                        Margin="0,0,10,0"/>
              <StackPanel Grid.Column="2" Orientation="Horizontal">
                <Button x:Name="btnTickAll" Content="Tick all shown"
                        Style="{StaticResource SmallButton}"/>
                <Button x:Name="btnTickNone" Content="Tick none"
                        Style="{StaticResource SmallButton}"/>
              </StackPanel>
            </Grid>
            <TextBlock x:Name="tbRowStatus" Grid.Row="1"
                       Style="{StaticResource Hint}" Margin="0,0,0,8"/>
            <ScrollViewer Grid.Row="2" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spRows"/>
            </ScrollViewer>
          </Grid>
        </Border>
      </TabItem>

      <TabItem x:Name="tabDevices">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="*"/>
            </Grid.RowDefinitions>
            <Grid Grid.Row="0" Margin="0,0,0,8">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBox x:Name="tbDeviceFilter" Grid.Column="0"
                       Margin="0,0,10,0"
                       ToolTip="Type part of a device name, a type, a circuit or a room."/>
              <CheckBox x:Name="cbOnlyLost" Grid.Column="1"
                        VerticalAlignment="Center"
                        VerticalContentAlignment="Center"
                        ToolTip="Show only the devices no room could be found for. These are the ones worth looking at.">
                <TextBlock Text="Only devices with no room"
                           Style="{StaticResource Label}"/>
              </CheckBox>
            </Grid>
            <TextBlock x:Name="tbDeviceStatus" Grid.Row="1"
                       Style="{StaticResource Hint}" Margin="0,0,0,8"/>
            <ScrollViewer Grid.Row="2" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spDevices"/>
            </ScrollViewer>
          </Grid>
        </Border>
      </TabItem>

      <TabItem x:Name="tabOverview">
        <Border Style="{StaticResource Card}">
          <ScrollViewer VerticalScrollBarVisibility="Auto">
            <TextBlock x:Name="tbOverview" TextWrapping="Wrap"
                       FontFamily="Consolas" FontSize="11.5"/>
          </ScrollViewer>
        </Border>
      </TabItem>
    </TabControl>

    <Border Grid.Row="2" Background="White" BorderBrush="#E2E8F0"
            BorderThickness="0,1,0,0" Padding="18,12" Margin="0,12,0,0">
      <DockPanel>
        <StackPanel DockPanel.Dock="Right" Orientation="Horizontal"
                    VerticalAlignment="Center">
          <Button x:Name="btnCopy" Content="Copy report"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnCsv" Content="Export CSV"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnClose" Content="Close" IsCancel="True"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,10,0"/>
          <Button x:Name="btnWrite" Content="Write the ticked values"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <Border x:Name="brdBanner" CornerRadius="6" Padding="10,7"
                BorderThickness="1" VerticalAlignment="Center"
                Margin="0,0,16,0" Background="#F0FDF4"
                BorderBrush="#BBF7D0">
          <TextBlock x:Name="tbBanner" TextWrapping="Wrap" FontSize="11.5"
                     Foreground="#166534"/>
        </Border>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""

STATUS_FILTERS = (
    ("all", "Everything"),
    (STATUS_WRITE, "Only what will be written"),
    (STATUS_KEEP, "Only values kept as they are"),
    (STATUS_SAME, "Only ones already correct"),
    (STATUS_NO_ROOMS, "Only ones with no room found"),
    (STATUS_NO_DEVICES, "Only circuits with nothing on them"),
    (STATUS_NO_PARAM, "Only ones without the parameter"),
    ("partial", "Only ones where a device has no room"),
)

STATUS_STYLE = {
    STATUS_WRITE: ("RowCardWrite", "RowGood"),
    STATUS_SAME: ("RowCard", "RowValue"),
    STATUS_KEEP: ("RowCardWarn", "RowWarn"),
    STATUS_NO_ROOMS: ("RowCardWarn", "RowWarn"),
    STATUS_NO_DEVICES: ("RowCardWarn", "RowWarn"),
    STATUS_NO_PARAM: ("RowCardBad", "RowBad"),
}


class ResultWindow(forms.WPFWindow):
    """Every circuit, the rooms found, and the value it would be given."""

    def __init__(self, changes, values, finder):
        forms.WPFWindow.__init__(self, RESULT_XAML, literal_string=True)
        self.changes = changes
        self.values = values
        self.finder = finder
        self.shown = []
        self.choice = "close"
        self.exported = []
        self.ready = False
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass
        for key, label in STATUS_FILTERS:
            self.cbStatusFilter.Items.Add(label)
        self.cbStatusFilter.SelectedIndex = 0
        self.cbStatusFilter.SelectionChanged += self.on_filter_changed
        self.tbFilter.TextChanged += self.on_filter_changed
        self.tbDeviceFilter.TextChanged += self.on_device_filter_changed
        self.cbOnlyLost.Checked += self.on_device_filter_changed
        self.cbOnlyLost.Unchecked += self.on_device_filter_changed
        self.btnTickAll.Click += self.tick_handler(True)
        self.btnTickNone.Click += self.tick_handler(False)
        self.btnCopy.Click += self.on_copy
        self.btnCsv.Click += self.on_csv
        self.btnWrite.Click += self.on_write
        self.btnClose.Click += self.on_close
        self.ready = True
        self.refresh()

    # ----- counts and text ----------------------------------------------------
    def counts(self):
        found = {}
        for change in self.changes:
            found[change["status"]] = found.get(change["status"], 0) + 1
        return found

    def ticked(self):
        return [change for change in self.changes if change["include"]]

    def all_devices(self):
        rows = []
        for change in self.changes:
            for device in change["devices"]:
                rows.append((change, device))
        return rows

    def refresh(self):
        counts = self.counts()
        self.tabCircuits.Header = "Circuits ({})".format(len(self.changes))
        self.tabDevices.Header = "Devices ({})".format(
            len(self.all_devices()))
        self.tabOverview.Header = "Overview"
        lost = sum(change["missing"] for change in self.changes)
        self.tbHeadline.Text = (
            "{} circuit(s) checked - {} to write, {} already correct"
            "{}. Nothing is written until you say so.".format(
                len(self.changes), counts.get(STATUS_WRITE, 0),
                counts.get(STATUS_SAME, 0),
                ", {} device(s) with no room".format(lost) if lost else ""
            )
        )
        self.tbOverview.Text = u"\n".join(self.report_lines())
        self.btnWrite.IsEnabled = counts.get(STATUS_WRITE, 0) > 0
        self.show_rows()
        self.show_devices()
        self.update_banner()

    def report_lines(self):
        counts = self.counts()
        values = self.values
        order_label = dict(ROOM_ORDERS).get(values["order"], values["order"])
        format_label = dict(
            (key, label) for key, label, _ in NAME_FORMATS
        ).get(values["name_format"], values["name_format"])
        lines = [
            u"Parameter written: {}".format(values["parameter"]),
            u"Values already there are {}.".format(
                u"replaced" if values["overwrite"] else u"kept"),
            u"Each room written as: {}".format(format_label),
            u"Rooms listed: {}".format(order_label),
            u"Joined with: {!r}".format(values["separator"]),
            u"",
            u"Circuits checked: {}".format(len(self.changes)),
        ]
        for key, label in STATUS_FILTERS:
            if key in ("all", "partial"):
                continue
            lines.append(u"  {:<44} {}".format(
                STATUS_TEXT.get(key, key), counts.get(key, 0)))
        lines.append(u"")
        devices = self.all_devices()
        lost = sum(change["missing"] for change in self.changes)
        lines.append(u"Devices on those circuits: {}".format(len(devices)))
        lines.append(u"  with no room found: {}".format(lost))
        lines.append(u"")
        lines.append(u"Rooms were read from this model{}.".format(
            u" and from {} linked model(s)".format(self.finder.link_count)
            if self.finder.link_count else u""))
        rooms = {}
        for change in self.changes:
            for entry in change["entries"]:
                rooms[entry["text"]] = rooms.get(entry["text"], 0) + 1
        lines.append(u"")
        lines.append(u"Rooms named, and how many circuits reach them:")
        if not rooms:
            lines.append(u"  none")
        for text in sorted(rooms, key=natural_key):
            lines.append(u"  {:<44} {}".format(text, rooms[text]))
        return lines

    def update_banner(self, message=None, good=None):
        if message is None:
            counts = self.counts()
            problems = (counts.get(STATUS_NO_ROOMS, 0)
                        + counts.get(STATUS_NO_PARAM, 0))
            lost = sum(change["missing"] for change in self.changes)
            good = problems == 0 and lost == 0
            message = u"{} circuit(s) ticked to write.".format(
                len(self.ticked()))
            if problems or lost:
                message += u" {} circuit(s) found no room, and {} device(s) " \
                           u"could not be placed in one.".format(
                               counts.get(STATUS_NO_ROOMS, 0), lost)
        if good:
            self.brdBanner.Background = media_brush(0xF0, 0xFD, 0xF4)
            self.brdBanner.BorderBrush = media_brush(0xBB, 0xF7, 0xD0)
        else:
            self.brdBanner.Background = media_brush(0xFF, 0xFB, 0xEB)
            self.brdBanner.BorderBrush = media_brush(0xFD, 0xE6, 0x8A)
        self.tbBanner.Style = self.FindResource(
            "RowGood" if good else "RowWarn")
        self.tbBanner.Text = message

    # ----- the circuit rows ----------------------------------------------------
    def current_status_filter(self):
        index = self.cbStatusFilter.SelectedIndex
        if index < 0 or index >= len(STATUS_FILTERS):
            return "all"
        return STATUS_FILTERS[index][0]

    def filtered(self):
        text = (self.tbFilter.Text or "").strip().lower()
        wanted = self.current_status_filter()
        found = []
        for change in self.changes:
            if wanted == "partial":
                if not change["missing"]:
                    continue
            elif wanted != "all" and change["status"] != wanted:
                continue
            if text:
                haystack = u" ".join((
                    change["label"], change["panel"], change["number"],
                    change["load"], change["current"], change["wanted"],
                    str(change["id"]),
                )).lower()
                if text not in haystack:
                    continue
            found.append(change)
        return found

    def show_rows(self):
        self.spRows.Children.Clear()
        self.shown = self.filtered()
        note = u"Showing {} of {} circuit(s).".format(
            min(len(self.shown), MAX_TABLE_ROWS), len(self.changes))
        if len(self.shown) > MAX_TABLE_ROWS:
            note += (u" Only the first {} are drawn - narrow the filter, or "
                     u"use Export CSV for the whole list.".format(
                         MAX_TABLE_ROWS))
        self.tbRowStatus.Text = note
        if not self.shown:
            self.spRows.Children.Add(self.message_row(
                "Nothing matches the filter.", "RowCard", "RowValue"))
            return
        for change in self.shown[:MAX_TABLE_ROWS]:
            self.spRows.Children.Add(self.make_row(change))

    def make_row(self, change):
        card_style, text_style = STATUS_STYLE.get(
            change["status"], ("RowCard", "RowValue"))
        card = Border()
        card.Style = self.FindResource(card_style)
        grid = Grid()
        for spec in (GridLength(24), GridLength(1.3, GridUnitType.Star),
                     GridLength(1, GridUnitType.Star), GridLength(140),
                     GridLength(104)):
            column = ColumnDefinition()
            column.Width = spec
            grid.ColumnDefinitions.Add(column)

        tick = CheckBox()
        tick.IsChecked = change["include"]
        tick.VerticalAlignment = VerticalAlignment.Center
        tick.IsEnabled = change["status"] == STATUS_WRITE
        tick.ToolTip = ("Write this one" if tick.IsEnabled
                        else STATUS_TEXT.get(change["status"], ""))
        tick.Click += self.tick_row_handler(change, tick)

        details = StackPanel()
        title = TextBlock()
        title.Style = self.FindResource("RowTitle")
        title.Text = change["label"]
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        pieces = [u"{} device(s)".format(change["device_count"])]
        if change["missing"]:
            pieces.append(u"{} with no room".format(change["missing"]))
        if change["load"]:
            pieces.append(change["load"])
        pieces.append(u"ID {}".format(change["id"]))
        meta.Text = u"  |  ".join(pieces)
        details.Children.Add(title)
        details.Children.Add(meta)

        value = StackPanel()
        new_text = TextBlock()
        new_text.Style = self.FindResource(text_style)
        new_text.Text = change["wanted"] or u"(nothing to write)"
        value.Children.Add(new_text)
        if change["current"] and change["current"] != change["wanted"]:
            old_text = TextBlock()
            old_text.Style = self.FindResource("RowMeta")
            old_text.Text = u"was: {}".format(change["current"])
            value.Children.Add(old_text)

        status_text = TextBlock()
        status_text.Style = self.FindResource(text_style)
        status_text.Text = STATUS_TEXT.get(change["status"],
                                           change["status"])

        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        select_button = Button()
        select_button.Content = "Devices"
        select_button.Style = self.FindResource("SmallButton")
        select_button.ToolTip = "Select this circuit's devices in Revit"
        select_button.Click += self.focus_handler(change, False)
        zoom_button = Button()
        zoom_button.Content = "Zoom"
        zoom_button.Style = self.FindResource("SmallButton")
        zoom_button.ToolTip = "Zoom the view to this circuit's devices"
        zoom_button.Click += self.focus_handler(change, True)
        buttons.Children.Add(select_button)
        buttons.Children.Add(zoom_button)

        for column, element in enumerate(
                (tick, details, value, status_text, buttons)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        return card

    # ----- the device rows -----------------------------------------------------
    def filtered_devices(self):
        text = (self.tbDeviceFilter.Text or "").strip().lower()
        only_lost = self.cbOnlyLost.IsChecked == True
        found = []
        for change, device in self.all_devices():
            if only_lost and device["room"] is not None:
                continue
            if text:
                room = device["room"]
                haystack = u" ".join((
                    device["name"], device["type_name"], change["label"],
                    room["label"] if room else u"", str(device["id"]),
                )).lower()
                if text not in haystack:
                    continue
            found.append((change, device))
        return found

    def show_devices(self):
        self.spDevices.Children.Clear()
        rows = self.filtered_devices()
        note = u"Showing {} of {} device(s).".format(
            min(len(rows), MAX_TABLE_ROWS), len(self.all_devices()))
        if len(rows) > MAX_TABLE_ROWS:
            note += u" Only the first {} are drawn.".format(MAX_TABLE_ROWS)
        self.tbDeviceStatus.Text = note
        if not rows:
            self.spDevices.Children.Add(self.message_row(
                "No devices match the filter.", "RowCard", "RowValue"))
            return
        for change, device in rows[:MAX_TABLE_ROWS]:
            self.spDevices.Children.Add(self.make_device_row(change, device))

    def make_device_row(self, change, device):
        room = device["room"]
        card = Border()
        card.Style = self.FindResource(
            "RowCard" if room is not None else "RowCardWarn")
        grid = Grid()
        for spec in (GridLength(1.2, GridUnitType.Star), GridLength(150),
                     GridLength(1, GridUnitType.Star), GridLength(104)):
            column = ColumnDefinition()
            column.Width = spec
            grid.ColumnDefinitions.Add(column)

        details = StackPanel()
        title = TextBlock()
        title.Style = self.FindResource("RowTitle")
        title.Text = u"{}   -   {}".format(device["name"],
                                           device["type_name"])
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        meta.Text = u"ID {}".format(device["id"])
        details.Children.Add(title)
        details.Children.Add(meta)

        circuit_text = TextBlock()
        circuit_text.Style = self.FindResource("RowValue")
        circuit_text.Text = change["label"]

        room_text = TextBlock()
        room_text.Style = self.FindResource(
            "RowValue" if room is not None else "RowWarn")
        if room is None:
            room_text.Text = u"no room found"
        else:
            text = room["label"]
            if room["source"]:
                text += u"  (in {})".format(room["source"])
            if room.get("how") and room["how"] != "its own point":
                text += u"  [found {}]".format(room["how"])
            room_text.Text = text

        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        zoom_button = Button()
        zoom_button.Content = "Zoom"
        zoom_button.Style = self.FindResource("SmallButton")
        zoom_button.Click += self.device_focus_handler(device)
        buttons.Children.Add(zoom_button)

        for column, element in enumerate(
                (details, circuit_text, room_text, buttons)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        return card

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

    # ----- buttons --------------------------------------------------------------
    def tick_row_handler(self, change, box):
        def handler(sender, args):
            change["include"] = box.IsChecked == True
            self.update_banner()
            try:
                args.Handled = True
            except Exception:
                pass
        return handler

    def tick_handler(self, state):
        def handler(sender, args):
            for change in (self.shown or self.changes):
                if change["status"] == STATUS_WRITE:
                    change["include"] = state
            self.show_rows()
            self.update_banner()
        return handler

    def focus(self, elements, zoom, label):
        try:
            element_ids = List[ElementId]()
            for element in elements:
                element_ids.Add(element.Id)
            if element_ids.Count == 0:
                self.update_banner(
                    u"{} has nothing to select.".format(label), False)
                return
            uidoc.Selection.SetElementIds(element_ids)
            if zoom:
                uidoc.ShowElements(element_ids)
            self.update_banner(
                u"{} selected in Revit - move or close this window to look "
                u"at it.".format(label), True)
        except Exception as error:
            self.update_banner(
                u"Revit could not show it: {}".format(error), False)

    def focus_handler(self, change, zoom):
        def handler(sender, args):
            self.focus([item["device"] for item in change["devices"]],
                       zoom, change["label"])
            try:
                args.Handled = True
            except Exception:
                pass
        return handler

    def device_focus_handler(self, device):
        def handler(sender, args):
            self.focus([device["device"]], True, device["name"])
            try:
                args.Handled = True
            except Exception:
                pass
        return handler

    def on_filter_changed(self, sender, args):
        if self.ready:
            self.show_rows()

    def on_device_filter_changed(self, sender, args):
        if self.ready:
            self.show_devices()

    def csv_rows(self):
        header = (
            "Circuit", "Panel", "Circuit number", "Load name", "Devices",
            "Devices with no room", "Rooms", "Current value", "New value",
            "Status", "Circuit ID"
        )
        rows = []
        for change in self.changes:
            rows.append((
                change["label"], change["panel"], change["number"],
                change["load"], str(change["device_count"]),
                str(change["missing"]),
                u" | ".join(entry["text"] for entry in change["entries"]),
                change["current"], change["wanted"],
                STATUS_TEXT.get(change["status"], change["status"]),
                str(change["id"]),
            ))
        return header, rows

    def device_csv_rows(self):
        header = ("Circuit", "Device", "Family and type", "Room",
                  "Room number", "Room source", "Found by", "Device ID")
        rows = []
        for change, device in self.all_devices():
            room = device["room"]
            rows.append((
                change["label"], device["name"], device["type_name"],
                room["name"] if room else u"",
                room["number"] if room else u"",
                (room["source"] or u"this model") if room else u"",
                room.get("how", u"") if room else u"",
                str(device["id"]),
            ))
        return header, rows

    def on_copy(self, sender, args):
        header, rows = self.csv_rows()
        lines = list(self.report_lines())
        lines.append(u"")
        lines.append(u"\t".join(header))
        for row in rows:
            lines.append(u"\t".join(row))
        try:
            Clipboard.SetText(u"\n".join(lines))
            self.update_banner(
                u"Report and {} circuit(s) copied - paste straight into "
                u"Excel, the columns are tab separated.".format(len(rows)),
                True)
        except Exception as error:
            self.update_banner(u"Could not copy: {}".format(error), False)

    def on_csv(self, sender, args):
        path = ask_export_path()
        if not path:
            return
        header, rows = self.csv_rows()
        try:
            write_csv(path, header, rows)
            device_path = path[:-4] + " - devices.csv" \
                if path.lower().endswith(".csv") else path + " - devices.csv"
            device_header, device_rows = self.device_csv_rows()
            write_csv(device_path, device_header, device_rows)
        except Exception as error:
            self.update_banner(
                u"Could not write the file: {}".format(error), False)
            return
        self.exported.append(path)
        self.exported.append(device_path)
        self.update_banner(
            u"{} circuit(s) and {} device(s) exported, next to each "
            u"other: {}".format(len(rows), len(device_rows), path), True)

    def on_write(self, sender, args):
        if not self.ticked():
            self.update_banner("Tick at least one circuit to write.", False)
            return
        self.choice = "write"
        self.Close()

    def on_close(self, sender, args):
        self.choice = "close"
        self.Close()

# =============================================================================
# Export
# =============================================================================
def csv_value(value):
    text = u"" if value is None else u"{}".format(value)
    if any(character in text for character in (u",", u'"', u"\n", u"\r")):
        return u'"{}"'.format(text.replace(u'"', u'""'))
    return text


def write_csv(path, header, rows):
    """UTF-8 with a byte-order mark, so Excel reads the accents correctly."""
    import codecs
    stream = codecs.open(path, "w", "utf-8-sig")
    try:
        stream.write(u",".join(csv_value(item) for item in header))
        stream.write(u"\r\n")
        for row in rows:
            stream.write(u",".join(csv_value(item) for item in row))
            stream.write(u"\r\n")
    finally:
        stream.close()
    return path


def ask_export_path():
    name = "Circuit room names"
    try:
        if doc.Title:
            name = "{} - Circuit room names".format(doc.Title)
    except Exception:
        pass
    try:
        return forms.save_file(
            file_ext="csv", default_name="{}.csv".format(
                name.replace(":", "-")))
    except Exception:
        pass
    import os
    try:
        return os.path.join(os.path.expanduser("~"),
                            "Circuit room names.csv")
    except Exception:
        return None




# =============================================================================
# Writing the values (the only part that changes the model)
# =============================================================================
def write_values(changes, values):
    """Set the parameter on the ticked circuits, all in one transaction."""
    summary = {"written": 0, "failures": [], "committed": False,
               "failure": ""}
    parameter_name = values["parameter"]
    transaction = Transaction(doc, "Circuit room names")
    transaction.Start()
    try:
        for change in changes:
            parameter = find_parameter(
                change["system"], parameter_name, "instance")
            if parameter is None:
                summary["failures"].append(
                    u"{}: the parameter is no longer there".format(
                        change["label"]))
                continue
            try:
                if parameter.Set(change["wanted"]):
                    summary["written"] += 1
                else:
                    summary["failures"].append(
                        u"{}: Revit would not take the value".format(
                            change["label"]))
            except Exception as error:
                summary["failures"].append(
                    u"{}: {}".format(change["label"], error))
        transaction.Commit()
        summary["committed"] = True
    except Exception as error:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        summary["failure"] = u"{}".format(error)
    return summary


# =============================================================================
# MAIN
# =============================================================================
# ---- 1. anything selected? the panels among them narrow the scope -----------
selected_panel_ids = set()
try:
    for element_id in uidoc.Selection.GetElementIds():
        element = doc.GetElement(element_id)
        if element is None:
            continue
        if is_category(element, BuiltInCategory.OST_ElectricalEquipment):
            selected_panel_ids.add(element_id_value(element_id))
except Exception:
    selected_panel_ids = set()

# ---- 2. the dialog -----------------------------------------------------------
stored_settings, user_config = load_settings()
setup = SetupWindow(stored_settings, selected_panel_ids)
setup.ShowDialog()
values = setup.values
if values is None:
    script.exit()
save_settings(user_config, values_to_settings(values))

# ---- 3. work out what would change ------------------------------------------
finder = RoomFinder(values["use_links"], values["use_spaces"], active_phase())
if values["use_links"] and finder.link_count == 0:
    forms.alert(
        "No loaded linked models were found, so rooms can only come from "
        "this model. If the rooms live in the architectural link, load it "
        "and run this again.",
        title=TITLE, warn_icon=True
    )

circuits = values["circuits"]
progress_bar = None
try:
    progress_bar = forms.ProgressBar(
        title="Finding the rooms  ({value} of {max_value} circuits)",
        cancellable=True)
    progress_bar.__enter__()
except Exception:
    progress_bar = None


def report_progress(done, total):
    if progress_bar is None:
        return True
    try:
        progress_bar.update_progress(done, total)
        return not progress_bar.cancelled
    except Exception:
        return True


try:
    changes = build_changes(circuits, finder, values, report_progress)
finally:
    if progress_bar is not None:
        try:
            progress_bar.__exit__(None, None, None)
        except Exception:
            pass

if not changes:
    forms.alert("No circuits were checked.", title=TITLE, exitscript=True)

# ---- 4. show it, then write if asked ----------------------------------------
window = ResultWindow(changes, values, finder)
window.ShowDialog()

summary = None
if window.choice == "write":
    summary = write_values(
        [change for change in changes if change["include"]], values)
    if not summary["committed"]:
        forms.alert(
            "The values could not be written and nothing was changed:\n\n"
            "{}".format(summary["failure"]),
            title=TITLE, warn_icon=True
        )

# ---- 5. report ----------------------------------------------------------------
counts = {}
for change in changes:
    counts[change["status"]] = counts.get(change["status"], 0) + 1
lost = sum(change["missing"] for change in changes)

output.print_md("# Circuit room names")
output.print_md("- Parameter: **{}**".format(values["parameter"]))
output.print_md("- Circuits checked: **{}**".format(len(changes)))
for key, label in STATUS_FILTERS:
    if key in ("all", "partial"):
        continue
    if counts.get(key):
        output.print_md("- {}: **{}**".format(
            STATUS_TEXT.get(key, key), counts[key]))
if lost:
    output.print_md(
        "- Devices no room could be found for: **{}**".format(lost))

output.print_md("## Circuits")
output.print_md("| Circuit | Devices | Rooms | Current | New | Status |")
output.print_md("|---|---:|---|---|---|---|")
for change in changes[:400]:
    try:
        link = output.linkify(change["system"].Id, title=change["label"])
    except Exception:
        link = change["label"]
    output.print_md("| {} | {} | {} | {} | {} | {} |".format(
        link, change["device_count"],
        ", ".join(entry["text"] for entry in change["entries"]) or "-",
        change["current"] or "-", change["wanted"] or "-",
        STATUS_TEXT.get(change["status"], change["status"])
    ))
if len(changes) > 400:
    output.print_md("_... and {} more; use Export CSV for the full "
                    "list._".format(len(changes) - 400))

if lost:
    output.print_md("## Devices with no room")
    output.print_md("| Device | Circuit | Type |")
    output.print_md("|---|---|---|")
    shown = 0
    for change in changes:
        for device in change["devices"]:
            if device["room"] is not None:
                continue
            shown += 1
            if shown > 200:
                break
            try:
                link = output.linkify(device["device"].Id,
                                      title=device["name"])
            except Exception:
                link = device["name"]
            output.print_md("| {} | {} | {} |".format(
                link, change["label"], device["type_name"]))
        if shown > 200:
            break

message = [
    "{} circuit(s) checked.".format(len(changes)),
    "{} would be written, {} already correct.".format(
        counts.get(STATUS_WRITE, 0), counts.get(STATUS_SAME, 0)),
]
if lost:
    message.append(
        "{} device(s) could not be placed in a room - see the Devices tab "
        "and the output window.".format(lost))
if summary is not None and summary["committed"]:
    message.append("")
    message.append("{} circuit(s) written.".format(summary["written"]))
    if summary["failures"]:
        message.append("{} could not be written. First: {}".format(
            len(summary["failures"]), summary["failures"][0]))
    message.append("One Undo takes it all back.")
elif summary is None:
    message.append("")
    message.append("Nothing was written.")
if window.exported:
    message.append("")
    message.append("Exported to:")
    message.extend("  {}".format(path) for path in window.exported)
message.append("")
message.append("The full table is in the pyRevit output window.")

forms.alert("\n".join(message), title=TITLE)
