# -*- coding: utf-8 -*-
__title__ = "IP Rating by Location"
__doc__ = (
    "Set the IP rating of electrical equipment from the room it stands in. "
    "Rooms are read from this model and from the linked architectural "
    "models, matched against a rule list you own, and every change is shown "
    "for checking before anything is written."
)

# =============================================================================
# IP RATING BY LOCATION
# -----------------------------------------------------------------------------
# What it does
#   1. Finds the electrical equipment in the categories you tick.
#   2. Works out which room or space each item stands in. Rooms usually live
#      in a linked architectural model, so the links are searched as well,
#      and several sample points are tried per item (its own point, the
#      point pushed off the wall it is hosted on, and the point dropped to
#      about a metre above its level) because a device on a wall or at high
#      level often falls outside the room volume at its own location.
#   3. Matches the room name and number against your rule list, first match
#      wins, and works out the IP rating that rule gives.
#   4. Shows every item, the room found, the rule that matched and the value
#      that would be written. You tick what to write.
#   5. Writes the ticked values into the parameter you chose, in ONE
#      transaction, so a single Undo takes it all back.
#
# THE RULES ARE YOURS
#   The list below is a starting point for a typical building, NOT an
#   authority. IP ratings come from the project specification and from
#   BS 7671 / IEC 60529, and a wrong one is a safety matter. Read the list,
#   change it to suit the job, and check the preview before writing. The
#   tool only ever writes what your rules say.
#
# MODIFIES THE MODEL only when you press Write: it sets one text parameter
# on the ticked elements. Nothing else is touched.
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

TITLE = "IP Rating by Location"
MM_TO_FT = 1.0 / 304.8
EPSILON = 1e-9

# How far off a wall a hosted device is pushed when looking for its room.
WALL_OFFSET_MM = 300.0
# How far above its level a high-level item is sampled from.
LEVEL_SAMPLE_MM = 1000.0

# The categories offered in the dialog: (BuiltInCategory name, label, on).
EQUIPMENT_CATEGORIES = (
    ("OST_ElectricalEquipment", "Electrical equipment", True),
    ("OST_ElectricalFixtures", "Electrical fixtures", True),
    ("OST_LightingFixtures", "Lighting fixtures", False),
    ("OST_LightingDevices", "Lighting devices", False),
    ("OST_FireAlarmDevices", "Fire alarm devices", False),
    ("OST_DataDevices", "Data devices", False),
    ("OST_CommunicationDevices", "Communication devices", False),
    ("OST_SecurityDevices", "Security devices", False),
    ("OST_NurseCallDevices", "Nurse call devices", False),
    ("OST_TelephoneDevices", "Telephone devices", False),
    ("OST_MechanicalEquipment", "Mechanical equipment", False),
)

# -----------------------------------------------------------------------------
# The default rules. One rule per line:
#
#     word, another word, third word  =>  IP54
#
# The words on the left are matched, ignoring case, against the room's number
# and name joined together ("E12 Main Switchroom"). The first line that
# matches decides the rating, so put the specific rooms above the general
# ones. A line of * matches any room that nothing above it caught.
#
# Lines starting with # are notes and are ignored.
# -----------------------------------------------------------------------------
DEFAULT_RULES = u"""# IP rating by room - CHECK AND EDIT FOR THIS PROJECT.
# The first matching line wins, so specific rooms go above general ones.

# --- wet and washed-down -----------------------------------------------
pool, swimming, spa, jacuzzi                        => IP68
shower, wet room, car wash, washdown, wash down     => IP65
kitchen, scullery, pantry wash, dishwash            => IP55
laundry, cleaner, janitor, sluice, housekeeping     => IP55
toilet, wc, bathroom, ablution, changing, locker    => IP44

# --- outside and exposed -----------------------------------------------
external, outdoor, roof, terrace, balcony, canopy   => IP66
yard, compound, external plant, open car park       => IP66
loading bay, refuse, bin store, service yard        => IP65

# --- below ground --------------------------------------------------------
pit, sump, tank room, tunnel, culvert, manhole      => IP67

# --- plant and back of house -------------------------------------------
plant, pump, chiller, boiler, ahu, air handling, cooling, heat exchanger => IP54
generator, fuel, lpg, gas room                      => IP54
car park, parking, ramp                             => IP54
workshop, maintenance, store, storage, bin          => IP54
riser, duct, shaft                                  => IP31

# --- dry electrical rooms ----------------------------------------------
switchroom, switch room, substation, transformer    => IP31
electrical, lv room, mv room, hv room, panel room   => IP31
comms, data, server, ict, bms, control room         => IP31

# --- damp places, last: these words prefix many room names, so they must
#     not outrank the room type itself ("Basement Plant Room" is plant) ---
basement, undercroft, crawl, cellar                 => IP55

# --- anything else indoors ---------------------------------------------
*                                                   => IP20
"""

DEFAULT_SETTINGS = {
    "categories": "OST_ElectricalEquipment,OST_ElectricalFixtures",
    "parameter": "",               # the parameter name last written to
    "parameter_scope": "instance",  # instance / type
    "rules": DEFAULT_RULES,
    "use_links": "1",              # search linked models for rooms
    "use_spaces": "1",             # fall back to MEP spaces
    "overwrite": "0",              # replace a value that is already there
    "no_room": "skip",             # skip / value
    "no_room_value": "IP66",       # used when no_room is "value"
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


# =============================================================================
# The rules
# =============================================================================
class Rule(object):
    """One line of the rule list."""

    def __init__(self, line_number, words, value, catch_all):
        self.line_number = line_number
        self.words = words          # already lower case
        self.value = value
        self.catch_all = catch_all
        self.hits = 0
        # A word matches where a word in the room name STARTS with it, not
        # anywhere inside one. Plain "contains" would read "ict" inside
        # "District Cooling Room" and call a plant room a comms room, while
        # matching the start of a word still catches the plurals and the
        # run-together spellings ("switchroom" finds "Switchrooms",
        # "plant" finds "Plantroom").
        self.patterns = []
        for word in words:
            if not word:
                continue
            try:
                self.patterns.append(
                    re.compile(r"\b" + re.escape(word), re.IGNORECASE))
            except Exception:
                self.patterns.append(None)

    @property
    def label(self):
        if self.catch_all:
            return u"* (any other room)"
        return u", ".join(self.words)

    def matches(self, haystack):
        if self.catch_all:
            return True
        for index, pattern in enumerate(self.patterns):
            if pattern is None:
                word = self.words[index]
                if word and word in haystack:
                    return True
            elif pattern.search(haystack):
                return True
        return False


def parse_rules(text):
    """Turn the rule text into Rule objects. Returns (rules, problems)."""
    rules = []
    problems = []
    for index, raw in enumerate((text or u"").splitlines()):
        line_number = index + 1
        line = raw.strip()
        if not line or line.startswith(u"#"):
            continue
        if u"=>" not in line:
            problems.append(
                u"Line {}: no '=>' in \"{}\" - a rule reads "
                u"word, word => IP54".format(line_number, line))
            continue
        left, right = line.split(u"=>", 1)
        value = right.strip()
        if not value:
            problems.append(
                u"Line {}: nothing to write after '=>'.".format(line_number))
            continue
        words = [word.strip().lower()
                 for word in left.split(u",") if word.strip()]
        catch_all = (left.strip() == u"*")
        if not words and not catch_all:
            problems.append(
                u"Line {}: no words to match before '=>'.".format(line_number))
            continue
        if catch_all:
            words = []
        rules.append(Rule(line_number, words, value, catch_all))
    if not rules and not problems:
        problems.append(u"There are no rules to apply.")
    return rules, problems


def match_rule(rules, haystack):
    """The first rule that matches, or None."""
    text = (haystack or u"").lower()
    for rule in rules:
        if rule.matches(text):
            return rule
    return None


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
# Working out what would change
# =============================================================================
# Every item ends up with one of these. Only "write" is ticked by default.
STATUS_WRITE = "write"
STATUS_SAME = "same"
STATUS_KEEP = "keep"
STATUS_NO_ROOM = "no_room"
STATUS_NO_RULE = "no_rule"
STATUS_NO_PARAM = "no_param"
STATUS_TYPE_CLASH = "type_clash"

STATUS_TEXT = {
    STATUS_WRITE: "will be written",
    STATUS_SAME: "already correct",
    STATUS_KEEP: "has a different value, kept",
    STATUS_NO_ROOM: "no room found",
    STATUS_NO_RULE: "no rule matched the room",
    STATUS_NO_PARAM: "the parameter is not on this item",
    STATUS_TYPE_CLASH: "this type is used in rooms needing different ratings",
}


def collect_elements(category_names):
    """The equipment in the ticked categories."""
    elements = []
    for name in category_names:
        builtin = getattr(BuiltInCategory, name, None)
        if builtin is None:
            continue
        try:
            found = (FilteredElementCollector(doc)
                     .OfCategory(builtin)
                     .WhereElementIsNotElementType()
                     .ToElements())
        except Exception:
            continue
        for element in found:
            elements.append(element)
    return elements


def build_changes(elements, rules, finder, values, progress=None):
    """One record per item: where it is, what rule matched, what to write."""
    parameter_name = values["parameter"]
    scope = values["parameter_scope"]
    overwrite = values["overwrite"]
    changes = []
    total = max(len(elements), 1)
    for index, element in enumerate(elements):
        if progress is not None and not progress(index + 1, total):
            break
        room = finder.find(element)
        rule = None
        wanted = None
        status = None
        if room is None:
            if values["no_room"] == "value" and values["no_room_value"]:
                wanted = values["no_room_value"]
            else:
                status = STATUS_NO_ROOM
        else:
            rule = match_rule(rules, room["haystack"])
            if rule is None:
                status = STATUS_NO_RULE
            else:
                wanted = rule.value

        parameter = find_parameter(element, parameter_name, scope)
        current = parameter_text(parameter) if parameter is not None else ""
        if parameter is None:
            status = STATUS_NO_PARAM
        elif status is None:
            if current == wanted:
                status = STATUS_SAME
            elif current and not overwrite:
                status = STATUS_KEEP
            else:
                status = STATUS_WRITE

        changes.append({
            "element": element,
            "id": element_id_value(element.Id),
            "name": element_name(element),
            "type_name": family_and_type(element),
            "type_id": element_id_value(element.GetTypeId()),
            "level": level_name(element),
            "room": room,
            "rule": rule,
            "current": current,
            "wanted": wanted,
            "status": status,
            "include": status == STATUS_WRITE,
        })
    if scope == "type":
        flag_type_clashes(changes)
    # Count every room a rule matched, whatever happened to the parameter
    # afterwards: the count is there to show which rules the model actually
    # exercises when you are reviewing the list.
    for change in changes:
        if change["rule"] is not None:
            change["rule"].hits += 1
    return changes


def flag_type_clashes(changes):
    """A type parameter is shared, so one type cannot hold two ratings.

    Writing a type parameter changes every instance of that type at once.
    Where the instances sit in rooms that want different ratings, there is
    no right answer, so those are flagged and left alone."""
    wanted_by_type = {}
    for change in changes:
        if change["wanted"] is None:
            continue
        wanted_by_type.setdefault(change["type_id"], set()).add(
            change["wanted"])
    for change in changes:
        values = wanted_by_type.get(change["type_id"])
        if values is not None and len(values) > 1:
            change["status"] = STATUS_TYPE_CLASH
            change["include"] = False


def type_instance_counts(changes):
    """How many items share each type, for the type-parameter warning."""
    counts = {}
    for change in changes:
        counts[change["type_id"]] = counts.get(change["type_id"], 0) + 1
    return counts


# =============================================================================
# SETUP DIALOG (WPF)
# =============================================================================
SETUP_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="IP Rating by Location" Width="1000" SizeToContent="Height"
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
          <Path Stroke="White" StrokeThickness="2.4" StrokeLineJoin="Round"
                StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Data="M 15,3 C 9,9 5,13 5,17 a 10,10 0 0 0 20,0 c 0,-4 -4,-8 -10,-14 z"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="IP Rating by Location" FontSize="20"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock Text="Set the IP rating of electrical equipment from the room it stands in"
                     FontSize="12" Foreground="#DBEAFE" Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
    <Grid Margin="20,18,20,4">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="1.05*"/>
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
              <TextBlock Text="Which equipment"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <CheckBox x:Name="cbSelectionOnly" Visibility="Collapsed"
                      ToolTip="Work on what you had selected when you started, instead of every item in the ticked categories.">
              <TextBlock x:Name="tbSelectionText" FontWeight="SemiBold"
                         Foreground="#334155"/>
            </CheckBox>
            <StackPanel x:Name="spCategories"/>
            <TextBlock x:Name="tbCountText" Style="{StaticResource Hint}"/>
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
            <StackPanel Orientation="Horizontal" Margin="0,0,0,10">
              <RadioButton x:Name="rbInstance" GroupName="Scope"
                           IsChecked="True"
                           ToolTip="Each item carries its own value. This is the safe choice.">
                <TextBlock Text="Instance parameter"
                           Foreground="#334155" FontWeight="SemiBold"/>
              </RadioButton>
              <RadioButton x:Name="rbType" GroupName="Scope"
                           ToolTip="The value belongs to the family type, so every instance of that type shares it. Types used in rooms that need different ratings are flagged and left alone.">
                <TextBlock Text="Type parameter" Foreground="#334155"
                           FontWeight="SemiBold"/>
              </RadioButton>
            </StackPanel>
            <TextBlock Text="Parameter" Style="{StaticResource Label}"/>
            <ComboBox x:Name="cbParameter"
                      ToolTip="Only text parameters that can be written are listed, with how many of the items have each one."/>
            <TextBlock x:Name="tbParameterHint" Style="{StaticResource Hint}"/>
            <CheckBox x:Name="cbOverwrite" Margin="0,12,0,0"
                      ToolTip="Off: an item that already has something in this parameter is listed but left alone. On: the rule's value replaces it.">
              <TextBlock Text="Replace values that are already there"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <TextBlock Text="When no room can be found"
                       Style="{StaticResource Label}" Margin="0,6,0,5"/>
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="110"/>
              </Grid.ColumnDefinitions>
              <ComboBox x:Name="cbNoRoom" Grid.Column="0"
                        ToolTip="Equipment outside the building, or outside every room volume, finds no room. Skipping is the safe default: the item is listed so you can look at it.">
                <ComboBoxItem Content="Skip it and list it" Tag="skip"/>
                <ComboBoxItem Content="Treat it as outside and write"
                              Tag="value"/>
              </ComboBox>
              <TextBox x:Name="tbNoRoomValue" Grid.Column="2" Height="30"
                       ToolTip="The rating written when no room is found and the option above says to write one."/>
            </Grid>
            <CheckBox x:Name="cbLinks" Margin="0,12,0,0"
                      ToolTip="Rooms normally live in the linked architectural model, so this is usually needed.">
              <TextBlock Text="Look for rooms in linked models"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>
            <CheckBox x:Name="cbSpaces" Margin="0,0,0,0"
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
            <DockPanel Margin="0,0,0,10">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="3" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="The rules" Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <Border Background="#FFFBEB" BorderBrush="#FDE68A"
                    BorderThickness="1" CornerRadius="6" Padding="10,8"
                    Margin="0,0,0,10">
              <TextBlock TextWrapping="Wrap" Foreground="#92400E"
                         FontSize="11.5"
                         Text="This list is a starting point, not an authority. IP ratings come from the project specification and from BS 7671 / IEC 60529. Read it, change it to suit the job, and check the preview before writing."/>
            </Border>
            <TextBlock Style="{StaticResource Label}"
                       Text="word, word, word  =>  IP54      (first match wins, * catches the rest, # is a note)"/>
            <TextBox x:Name="tbRules" Height="300"
                     AcceptsReturn="True" AcceptsTab="False"
                     VerticalScrollBarVisibility="Auto"
                     HorizontalScrollBarVisibility="Auto"
                     FontFamily="Consolas" FontSize="11.5"
                     VerticalContentAlignment="Top"
                     ToolTip="Matched against the room number and name joined together, ignoring case."/>
            <Grid Margin="0,8,0,0">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBlock x:Name="tbRuleStatus" Grid.Column="0"
                         Style="{StaticResource Hint}" Margin="0,0,10,0"/>
              <Button x:Name="btnResetRules" Grid.Column="1"
                      Content="Reset to the defaults"
                      Style="{StaticResource SecondaryButton}"/>
            </Grid>
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
          <Button x:Name="btnRun" Content="Check the equipment"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <TextBlock VerticalAlignment="Center" TextWrapping="Wrap"
                   Foreground="#64748B" FontSize="11" Margin="0,0,16,0"
                   Text="Nothing is changed yet. Every item is listed with the room found and the value its rule gives, and you choose what to write."/>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


class SetupWindow(forms.WPFWindow):
    """Categories, the parameter, and the rules."""

    def __init__(self, settings, preselected):
        forms.WPFWindow.__init__(self, SETUP_XAML, literal_string=True)
        self.preselected = preselected
        self.values = None
        self.ready = False
        self.category_boxes = []
        self.parameter_names = []
        self.elements = []
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass

        wanted = set(
            name.strip() for name in settings["categories"].split(",")
            if name.strip()
        )
        for name, label, default_on in EQUIPMENT_CATEGORIES:
            box = CheckBox()
            text = TextBlock()
            text.Text = label
            box.Content = text
            box.IsChecked = (name in wanted) if wanted else default_on
            box.Checked += self.on_categories_changed
            box.Unchecked += self.on_categories_changed
            self.spCategories.Children.Add(box)
            self.category_boxes.append((name, box))

        if preselected:
            self.cbSelectionOnly.Visibility = Visibility.Visible
            self.tbSelectionText.Text = (
                "Only the {} item(s) selected in the model".format(
                    len(preselected))
            )
            self.cbSelectionOnly.IsChecked = True
            self.cbSelectionOnly.Checked += self.on_categories_changed
            self.cbSelectionOnly.Unchecked += self.on_categories_changed

        self.tbRules.Text = settings["rules"]
        self.cbOverwrite.IsChecked = settings["overwrite"] == "1"
        self.cbLinks.IsChecked = settings["use_links"] == "1"
        self.cbSpaces.IsChecked = settings["use_spaces"] == "1"
        self.tbNoRoomValue.Text = settings["no_room_value"]
        if settings["parameter_scope"] == "type":
            self.rbType.IsChecked = True
        else:
            self.rbInstance.IsChecked = True
        for index in range(self.cbNoRoom.Items.Count):
            if str(self.cbNoRoom.Items[index].Tag) == settings["no_room"]:
                self.cbNoRoom.SelectedIndex = index
        if self.cbNoRoom.SelectedIndex < 0:
            self.cbNoRoom.SelectedIndex = 0

        self.remembered_parameter = settings["parameter"]
        self.rbInstance.Checked += self.on_scope_changed
        self.rbType.Checked += self.on_scope_changed
        self.tbRules.TextChanged += self.on_rules_changed
        self.btnResetRules.Click += self.on_reset_rules
        self.btnRun.Click += self.on_run
        self.btnCancel.Click += self.on_cancel
        self.ready = True
        self.refresh_elements()
        self.update_rule_status()
        self.update_links_text()

    # ----- live refresh -------------------------------------------------------
    def chosen_categories(self):
        return [name for name, box in self.category_boxes
                if box.IsChecked == True]

    def use_selection(self):
        return (self.preselected
                and self.cbSelectionOnly.IsChecked == True)

    def current_scope(self):
        return "type" if self.rbType.IsChecked == True else "instance"

    def refresh_elements(self):
        """Collect the equipment and list the parameters it carries."""
        if not self.ready:
            return
        if self.use_selection():
            self.elements = list(self.preselected)
            self.tbCountText.Text = (
                "{} item(s) from the model selection.".format(
                    len(self.elements))
            )
        else:
            self.elements = collect_elements(self.chosen_categories())
            self.tbCountText.Text = (
                "{} item(s) in the ticked categories.".format(
                    len(self.elements))
            )
        self.refresh_parameters()

    def refresh_parameters(self):
        scope = self.current_scope()
        choices = parameter_choices(self.elements, scope)
        self.parameter_names = [name for name, _ in choices]
        self.cbParameter.Items.Clear()
        for name, count in choices:
            self.cbParameter.Items.Add(
                u"{}   (on {} of {})".format(name, count, len(self.elements))
            )
        if not choices:
            self.cbParameter.Items.Add(
                "(no writable text parameter on these items)")
            self.cbParameter.SelectedIndex = 0
            self.cbParameter.IsEnabled = False
            self.tbParameterHint.Text = (
                "Add a text parameter for the IP rating to these families, "
                "or to the project, and run this again."
            )
            return
        self.cbParameter.IsEnabled = True
        chosen = 0
        for index, name in enumerate(self.parameter_names):
            if name == self.remembered_parameter:
                chosen = index
                break
            lowered = name.lower()
            if "ip" in lowered and ("rating" in lowered or "ip" == lowered):
                chosen = index
        self.cbParameter.SelectedIndex = chosen
        if scope == "type":
            self.tbParameterHint.Text = (
                "A type parameter is shared by every instance of that type. "
                "Types whose instances sit in rooms needing different "
                "ratings are flagged in the preview and left alone."
            )
        else:
            self.tbParameterHint.Text = (
                "Only text parameters that can be written are listed."
            )

    def update_links_text(self):
        try:
            count = len(FilteredElementCollector(doc)
                        .OfClass(DB.RevitLinkInstance).ToElements())
        except Exception:
            count = 0
        loaded = 0
        try:
            for instance in (FilteredElementCollector(doc)
                             .OfClass(DB.RevitLinkInstance).ToElements()):
                if instance.GetLinkDocument() is not None:
                    loaded += 1
        except Exception:
            pass
        if count == 0:
            self.tbLinkText.Text = "No linked models in this project."
        else:
            self.tbLinkText.Text = (
                "{} linked model(s), {} loaded.".format(count, loaded)
            )

    def update_rule_status(self):
        rules, problems = parse_rules(self.tbRules.Text)
        if problems:
            self.tbRuleStatus.Text = u"{} rule(s), {} problem(s): {}".format(
                len(rules), len(problems), u"  ".join(problems[:3])
            )
        else:
            self.tbRuleStatus.Text = (
                u"{} rule(s) read. The last one that can match is \"{}\" "
                u"=> {}.".format(len(rules), rules[-1].label, rules[-1].value)
                if rules else u"No rules yet."
            )

    def on_categories_changed(self, sender, args):
        self.refresh_elements()

    def on_scope_changed(self, sender, args):
        if self.ready:
            self.refresh_parameters()

    def on_rules_changed(self, sender, args):
        if self.ready:
            self.update_rule_status()

    def on_reset_rules(self, sender, args):
        self.tbRules.Text = DEFAULT_RULES

    # ----- buttons ------------------------------------------------------------
    def on_run(self, sender, args):
        try:
            if not self.elements:
                raise ValueError(
                    "No equipment to work on. Tick at least one category "
                    "that has items in it.")
            if not self.parameter_names or not self.cbParameter.IsEnabled:
                raise ValueError(
                    "There is no writable text parameter on these items, so "
                    "there is nowhere to put the rating.")
            index = self.cbParameter.SelectedIndex
            if index < 0 or index >= len(self.parameter_names):
                raise ValueError("Choose the parameter to write.")
            rules, problems = parse_rules(self.tbRules.Text)
            if problems:
                raise ValueError(
                    "The rules have {} problem(s):\n\n{}".format(
                        len(problems), "\n".join(problems[:8])))
            no_room = str(self.cbNoRoom.SelectedItem.Tag)
            no_room_value = (self.tbNoRoomValue.Text or "").strip()
            if no_room == "value" and not no_room_value:
                raise ValueError(
                    "Enter the rating to use when no room is found, or set "
                    "that option back to skipping.")
            self.values = {
                "categories": self.chosen_categories(),
                "selection_only": self.use_selection(),
                "elements": self.elements,
                "parameter": self.parameter_names[index],
                "parameter_scope": self.current_scope(),
                "rules_text": self.tbRules.Text,
                "rules": rules,
                "overwrite": self.cbOverwrite.IsChecked == True,
                "use_links": self.cbLinks.IsChecked == True,
                "use_spaces": self.cbSpaces.IsChecked == True,
                "no_room": no_room,
                "no_room_value": no_room_value,
            }
            self.Close()
        except ValueError as error:
            forms.alert(str(error), title=TITLE, warn_icon=True)

    def on_cancel(self, sender, args):
        self.values = None
        self.Close()


def values_to_settings(values):
    return {
        "categories": ",".join(values["categories"]),
        "parameter": values["parameter"],
        "parameter_scope": values["parameter_scope"],
        "rules": values["rules_text"],
        "use_links": "1" if values["use_links"] else "0",
        "use_spaces": "1" if values["use_spaces"] else "0",
        "overwrite": "1" if values["overwrite"] else "0",
        "no_room": values["no_room"],
        "no_room_value": values["no_room_value"],
    }


# =============================================================================
# PREVIEW WINDOW (WPF) - nothing is written until you press Write
# =============================================================================
MAX_TABLE_ROWS = 400

RESULT_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="IP Rating by Location" Width="1180" Height="820"
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
          <Path Stroke="White" StrokeThickness="2.4" StrokeLineJoin="Round"
                StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                HorizontalAlignment="Center" VerticalAlignment="Center"
                Data="M 15,3 C 9,9 5,13 5,17 a 10,10 0 0 0 20,0 c 0,-4 -4,-8 -10,-14 z"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="IP rating by location" FontSize="18"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock x:Name="tbHeadline" FontSize="12" Foreground="#DBEAFE"
                     Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <TabControl Grid.Row="1" Margin="18,12,18,0" BorderThickness="0">
      <TabItem x:Name="tabChanges">
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
                       ToolTip="Type part of a name, a type, a room or a rating to narrow the list."/>
              <ComboBox x:Name="cbStatusFilter" Grid.Column="1" Width="230"
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

      <TabItem x:Name="tabRules">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="*"/>
            </Grid.RowDefinitions>
            <TextBlock Grid.Row="0" Style="{StaticResource Hint}"
                       Margin="0,0,0,8"
                       Text="Every rule, in the order they are tried, and how many rooms each one caught. A rule that caught nothing may be worded for rooms this model does not have."/>
            <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spRules"/>
            </ScrollViewer>
          </Grid>
        </Border>
      </TabItem>

      <TabItem x:Name="tabRooms">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="*"/>
            </Grid.RowDefinitions>
            <TextBlock Grid.Row="0" Style="{StaticResource Hint}"
                       Margin="0,0,0,8"
                       Text="The rooms the equipment was found in, and what each one resolved to. Worth a read: a room named in a way the rules do not expect is the usual reason for a wrong rating."/>
            <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spRooms"/>
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
    (STATUS_NO_ROOM, "Only ones with no room"),
    (STATUS_NO_RULE, "Only ones no rule matched"),
    (STATUS_NO_PARAM, "Only ones without the parameter"),
    (STATUS_TYPE_CLASH, "Only clashing types"),
)

STATUS_STYLE = {
    STATUS_WRITE: ("RowCardWrite", "RowGood"),
    STATUS_SAME: ("RowCard", "RowValue"),
    STATUS_KEEP: ("RowCardWarn", "RowWarn"),
    STATUS_NO_ROOM: ("RowCardWarn", "RowWarn"),
    STATUS_NO_RULE: ("RowCardWarn", "RowWarn"),
    STATUS_NO_PARAM: ("RowCardBad", "RowBad"),
    STATUS_TYPE_CLASH: ("RowCardBad", "RowBad"),
}


class ResultWindow(forms.WPFWindow):
    """Every item, the room found, and what would be written."""

    def __init__(self, changes, rules, values, finder):
        forms.WPFWindow.__init__(self, RESULT_XAML, literal_string=True)
        self.changes = changes
        self.rules = rules
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

    def refresh(self):
        counts = self.counts()
        self.tabChanges.Header = "Equipment ({})".format(len(self.changes))
        self.tabRules.Header = "Rules ({})".format(len(self.rules))
        self.tabRooms.Header = "Rooms ({})".format(len(self.room_rows()))
        self.tabOverview.Header = "Overview"
        self.tbHeadline.Text = (
            "{} item(s) checked - {} to write, {} already correct, {} left "
            "alone. Nothing is written until you say so.".format(
                len(self.changes), counts.get(STATUS_WRITE, 0),
                counts.get(STATUS_SAME, 0),
                len(self.changes) - counts.get(STATUS_WRITE, 0)
                - counts.get(STATUS_SAME, 0)
            )
        )
        self.tbOverview.Text = "\n".join(self.report_lines())
        self.btnWrite.IsEnabled = counts.get(STATUS_WRITE, 0) > 0
        self.show_rows()
        self.show_rules()
        self.show_rooms()
        self.update_banner()

    def report_lines(self):
        counts = self.counts()
        values = self.values
        lines = [
            u"Parameter written: {}  ({})".format(
                values["parameter"],
                "type parameter" if values["parameter_scope"] == "type"
                else "instance parameter"),
            u"Values already there are {}.".format(
                "replaced" if values["overwrite"] else "kept"),
            u"",
            u"Equipment checked: {}".format(len(self.changes)),
        ]
        for key, label in STATUS_FILTERS:
            if key == "all":
                continue
            lines.append(u"  {:<42} {}".format(
                STATUS_TEXT.get(key, key), counts.get(key, 0)))
        lines.append(u"")
        lines.append(u"Rooms were read from this model{}.".format(
            u" and from {} linked model(s)".format(self.finder.link_count)
            if self.finder.link_count else u""))
        if values["no_room"] == "value":
            lines.append(
                u"Equipment with no room was treated as outside and given "
                u"{}.".format(values["no_room_value"]))
        else:
            lines.append(u"Equipment with no room was skipped and listed.")
        lines.append(u"")
        lines.append(u"Ratings that would be written:")
        wanted = {}
        for change in self.changes:
            if change["status"] != STATUS_WRITE:
                continue
            wanted[change["wanted"]] = wanted.get(change["wanted"], 0) + 1
        if not wanted:
            lines.append(u"  nothing")
        for value in sorted(wanted):
            lines.append(u"  {:<10} {} item(s)".format(value, wanted[value]))
        return lines

    def update_banner(self, message=None, good=None):
        counts = self.counts()
        if message is None:
            writing = len(self.ticked())
            problems = (counts.get(STATUS_NO_ROOM, 0)
                        + counts.get(STATUS_NO_PARAM, 0)
                        + counts.get(STATUS_NO_RULE, 0)
                        + counts.get(STATUS_TYPE_CLASH, 0))
            good = problems == 0
            message = u"{} item(s) ticked to write.".format(writing)
            if problems:
                message += (u" {} item(s) need a look: no room, no rule, no "
                            u"parameter, or a shared type.".format(problems))
        if good:
            self.brdBanner.Background = media_brush(0xF0, 0xFD, 0xF4)
            self.brdBanner.BorderBrush = media_brush(0xBB, 0xF7, 0xD0)
        else:
            self.brdBanner.Background = media_brush(0xFF, 0xFB, 0xEB)
            self.brdBanner.BorderBrush = media_brush(0xFD, 0xE6, 0x8A)
        self.tbBanner.Style = self.FindResource(
            "RowGood" if good else "RowWarn")
        self.tbBanner.Text = message

    # ----- the rows -----------------------------------------------------------
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
            if wanted != "all" and change["status"] != wanted:
                continue
            if text:
                room = change["room"]
                haystack = u" ".join((
                    change["name"], change["type_name"], change["level"],
                    room["label"] if room else u"", change["current"],
                    change["wanted"] or u"", str(change["id"]),
                )).lower()
                if text not in haystack:
                    continue
            found.append(change)
        return found

    def show_rows(self):
        self.spRows.Children.Clear()
        self.shown = self.filtered()
        note = u"Showing {} of {} item(s).".format(
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
        for spec in (GridLength(24), GridLength(1, GridUnitType.Star),
                     GridLength(150), GridLength(150), GridLength(104)):
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
        title.Text = u"{}   -   {}".format(change["name"],
                                           change["type_name"])
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        room = change["room"]
        pieces = []
        if room:
            where = room["label"]
            if room["source"]:
                where += u"  (in {})".format(room["source"])
            if room["kind"] == "Space":
                where += u"  [space]"
            pieces.append(where)
            if room.get("how") and room["how"] != "its own point":
                pieces.append(u"found {}".format(room["how"]))
        else:
            pieces.append(u"no room found")
        if change["level"]:
            pieces.append(change["level"])
        if change["rule"] is not None:
            pieces.append(u"rule: {}".format(change["rule"].label))
        pieces.append(u"ID {}".format(change["id"]))
        meta.Text = u"  |  ".join(pieces)
        details.Children.Add(title)
        details.Children.Add(meta)

        change_text = TextBlock()
        change_text.Style = self.FindResource(text_style)
        current = change["current"] if change["current"] else u"(empty)"
        if change["wanted"] is None:
            change_text.Text = current
        else:
            change_text.Text = u"{}  →  {}".format(
                current, change["wanted"])

        status_text = TextBlock()
        status_text.Style = self.FindResource(text_style)
        status_text.Text = STATUS_TEXT.get(change["status"],
                                           change["status"])

        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        select_button = Button()
        select_button.Content = "Select"
        select_button.Style = self.FindResource("SmallButton")
        select_button.Click += self.focus_handler(change, False)
        zoom_button = Button()
        zoom_button.Content = "Zoom"
        zoom_button.Style = self.FindResource("SmallButton")
        zoom_button.Click += self.focus_handler(change, True)
        buttons.Children.Add(select_button)
        buttons.Children.Add(zoom_button)

        for column, element in enumerate(
                (tick, details, change_text, status_text, buttons)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        return card

    def show_rules(self):
        self.spRules.Children.Clear()
        for rule in self.rules:
            card = Border()
            card.Style = self.FindResource(
                "RowCard" if rule.hits else "RowCardWarn")
            grid = Grid()
            for spec in (GridLength(1, GridUnitType.Star), GridLength(90),
                         GridLength(120)):
                column = ColumnDefinition()
                column.Width = spec
                grid.ColumnDefinitions.Add(column)
            words = TextBlock()
            words.Style = self.FindResource("RowTitle")
            words.Text = u"line {}:  {}".format(rule.line_number, rule.label)
            value = TextBlock()
            value.Style = self.FindResource("RowValue")
            value.Text = rule.value
            hits = TextBlock()
            hits.Style = self.FindResource(
                "RowValue" if rule.hits else "RowWarn")
            hits.Text = (u"{} room match(es)".format(rule.hits)
                         if rule.hits else u"never matched")
            for column, element in enumerate((words, value, hits)):
                Grid.SetColumn(element, column)
                grid.Children.Add(element)
            card.Child = grid
            self.spRules.Children.Add(card)

    def room_rows(self):
        """Each distinct room found, with its rating and how many items."""
        rooms = {}
        for change in self.changes:
            room = change["room"]
            if room is None:
                continue
            key = (room["source"], room["id"])
            entry = rooms.get(key)
            if entry is None:
                entry = {"room": room, "count": 0, "values": set(),
                         "rule": change["rule"]}
                rooms[key] = entry
            entry["count"] += 1
            if change["wanted"]:
                entry["values"].add(change["wanted"])
        ordered = list(rooms.values())
        ordered.sort(key=lambda item: item["room"]["label"].lower())
        return ordered

    def show_rooms(self):
        self.spRooms.Children.Clear()
        rows = self.room_rows()
        if not rows:
            self.spRooms.Children.Add(self.message_row(
                "No rooms were found for any of the equipment.",
                "RowCardWarn", "RowWarn"))
            return
        for entry in rows:
            room = entry["room"]
            card = Border()
            card.Style = self.FindResource("RowCard")
            grid = Grid()
            for spec in (GridLength(1, GridUnitType.Star), GridLength(120),
                         GridLength(110)):
                column = ColumnDefinition()
                column.Width = spec
                grid.ColumnDefinitions.Add(column)
            title = TextBlock()
            title.Style = self.FindResource("RowTitle")
            title.Text = room["label"]
            meta = TextBlock()
            meta.Style = self.FindResource("RowMeta")
            bits = [room["kind"]]
            if room["source"]:
                bits.append(u"in {}".format(room["source"]))
            if entry["rule"] is not None:
                bits.append(u"rule: {}".format(entry["rule"].label))
            meta.Text = u"  |  ".join(bits)
            details = StackPanel()
            details.Children.Add(title)
            details.Children.Add(meta)
            value = TextBlock()
            value.Style = self.FindResource("RowValue")
            value.Text = u", ".join(sorted(entry["values"])) or u"-"
            count = TextBlock()
            count.Style = self.FindResource("RowValue")
            count.Text = u"{} item(s)".format(entry["count"])
            for column, element in enumerate((details, value, count)):
                Grid.SetColumn(element, column)
                grid.Children.Add(element)
            card.Child = grid
            self.spRooms.Children.Add(card)

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

    # ----- buttons -------------------------------------------------------------
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

    def focus_handler(self, change, zoom):
        def handler(sender, args):
            try:
                element_ids = List[ElementId]()
                element_ids.Add(change["element"].Id)
                uidoc.Selection.SetElementIds(element_ids)
                if zoom:
                    uidoc.ShowElements(element_ids)
                self.update_banner(
                    u"{} selected in Revit - move or close this window to "
                    u"look at it.".format(change["name"]), True)
            except Exception as error:
                self.update_banner(
                    u"Revit could not show it: {}".format(error), False)
            try:
                args.Handled = True
            except Exception:
                pass
        return handler

    def on_filter_changed(self, sender, args):
        if self.ready:
            self.show_rows()

    def csv_rows(self):
        header = (
            "Name", "Family and type", "Level", "Room", "Room number",
            "Room source", "Found by", "Rule line", "Rule", "Current value",
            "New value", "Status", "Element ID"
        )
        rows = []
        for change in self.changes:
            room = change["room"]
            rule = change["rule"]
            rows.append((
                change["name"], change["type_name"], change["level"],
                room["name"] if room else "", room["number"] if room else "",
                (room["source"] if room and room["source"] else "this model")
                if room else "",
                room.get("how", "") if room else "",
                str(rule.line_number) if rule else "",
                rule.label if rule else "",
                change["current"], change["wanted"] or "",
                STATUS_TEXT.get(change["status"], change["status"]),
                str(change["id"]),
            ))
        return header, rows

    def on_copy(self, sender, args):
        header, rows = self.csv_rows()
        lines = list(self.report_lines())
        lines.append(u"")
        lines.append(u"\t".join(header))
        for row in rows:
            lines.append(u"\t".join(item for item in row))
        try:
            Clipboard.SetText(u"\n".join(lines))
            self.update_banner(
                u"Report and {} row(s) copied - paste straight into Excel, "
                u"the columns are tab separated.".format(len(rows)), True)
        except Exception as error:
            self.update_banner(
                u"Could not copy: {}".format(error), False)

    def on_csv(self, sender, args):
        header, rows = self.csv_rows()
        path = ask_export_path()
        if not path:
            return
        try:
            write_csv(path, header, rows)
        except Exception as error:
            self.update_banner(
                u"Could not write the file: {}".format(error), False)
            return
        self.exported.append(path)
        self.update_banner(
            u"{} row(s) exported to {}".format(len(rows), path), True)

    def on_write(self, sender, args):
        if not self.ticked():
            self.update_banner("Tick at least one item to write.", False)
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
    name = "IP rating by location"
    try:
        if doc.Title:
            name = "{} - IP rating by location".format(doc.Title)
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
                            "IP rating by location.csv")
    except Exception:
        return None


# =============================================================================
# Writing the values (the only part that changes the model)
# =============================================================================
def write_values(changes, values):
    """Set the parameter on the ticked items, all in one transaction."""
    summary = {"written": 0, "types": 0, "items": 0, "failures": [],
               "committed": False, "failure": ""}
    parameter_name = values["parameter"]
    scope = values["parameter_scope"]
    done_types = set()
    transaction = Transaction(doc, "IP rating by location")
    transaction.Start()
    try:
        for change in changes:
            element = change["element"]
            wanted = change["wanted"]
            if wanted is None:
                continue
            if scope == "type":
                # One type, one value: setting it again for the next
                # instance would be the same write.
                if change["type_id"] in done_types:
                    summary["items"] += 1
                    continue
            parameter = find_parameter(element, parameter_name, scope)
            if parameter is None:
                summary["failures"].append(
                    u"{}: the parameter is no longer there".format(
                        change["name"]))
                continue
            try:
                if parameter.Set(wanted):
                    summary["written"] += 1
                    summary["items"] += 1
                    if scope == "type":
                        done_types.add(change["type_id"])
                        summary["types"] += 1
                else:
                    summary["failures"].append(
                        u"{}: Revit would not take the value".format(
                            change["name"]))
            except Exception as error:
                summary["failures"].append(
                    u"{}: {}".format(change["name"], error))
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
# ---- 1. anything already selected? ------------------------------------------
preselected = []
try:
    for element_id in uidoc.Selection.GetElementIds():
        element = doc.GetElement(element_id)
        if element is None:
            continue
        try:
            if element.Category is None:
                continue
        except Exception:
            continue
        preselected.append(element)
except Exception:
    preselected = []

# ---- 2. the dialog -----------------------------------------------------------
stored_settings, user_config = load_settings()
setup = SetupWindow(stored_settings, preselected)
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

elements = values["elements"]
progress_bar = None
try:
    progress_bar = forms.ProgressBar(
        title="Finding rooms  ({value} of {max_value})", cancellable=True)
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
    changes = build_changes(elements, values["rules"], finder, values,
                            report_progress)
finally:
    if progress_bar is not None:
        try:
            progress_bar.__exit__(None, None, None)
        except Exception:
            pass

if not changes:
    forms.alert("Nothing was checked.", title=TITLE, exitscript=True)

# ---- 4. show it, then write if asked ----------------------------------------
window = ResultWindow(changes, values["rules"], values, finder)
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

output.print_md("# IP rating by location")
output.print_md("- Parameter: **{}** ({})".format(
    values["parameter"],
    "type" if values["parameter_scope"] == "type" else "instance"))
output.print_md("- Equipment checked: **{}**".format(len(changes)))
for key, label in STATUS_FILTERS:
    if key == "all":
        continue
    if counts.get(key):
        output.print_md("- {}: **{}**".format(
            STATUS_TEXT.get(key, key), counts[key]))

output.print_md("## Rules")
output.print_md("| Line | Matches | Rating | Rooms caught |")
output.print_md("|---|---|---|---:|")
for rule in values["rules"]:
    output.print_md("| {} | {} | {} | {} |".format(
        rule.line_number, rule.label, rule.value, rule.hits))

listed = [change for change in changes
          if change["status"] in (STATUS_WRITE, STATUS_NO_ROOM,
                                  STATUS_NO_RULE, STATUS_TYPE_CLASH,
                                  STATUS_KEEP)]
if listed:
    output.print_md("## Equipment worth a look")
    output.print_md(
        "| Item | Room | Current | New | Status |")
    output.print_md("|---|---|---|---|---|")
    for change in listed[:300]:
        room = change["room"]
        try:
            link = output.linkify(change["element"].Id, title=change["name"])
        except Exception:
            link = change["name"]
        output.print_md("| {} | {} | {} | {} | {} |".format(
            link,
            room["label"] if room else "(none)",
            change["current"] or "-",
            change["wanted"] or "-",
            STATUS_TEXT.get(change["status"], change["status"])
        ))
    if len(listed) > 300:
        output.print_md("_... and {} more; use Export CSV for the full "
                        "list._".format(len(listed) - 300))

message = [
    "{} item(s) checked.".format(len(changes)),
    "{} would be written, {} already correct.".format(
        counts.get(STATUS_WRITE, 0), counts.get(STATUS_SAME, 0)),
]
if summary is not None and summary["committed"]:
    message.append("")
    if values["parameter_scope"] == "type":
        message.append(
            "{} type parameter(s) set, covering {} item(s).".format(
                summary["types"], summary["items"]))
    else:
        message.append("{} item(s) written.".format(summary["written"]))
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
