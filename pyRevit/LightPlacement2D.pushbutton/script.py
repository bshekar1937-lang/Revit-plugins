# -*- coding: utf-8 -*-
__title__ = "LIGHT\nPLACEMENT 2D"
__doc__ = (
    "Choose a lighting link, the levels to process and one 2D annotation "
    "family. Existing matching types are reused and missing types are created "
    "inside that family. A preview lets you place new symbols or update "
    "existing symbols at matching locations."
)

# =============================================================================
# LIGHT PLACEMENT 2D
#   Run              -> choose levels and one 2D symbol family:
#     * link       : the loaded link that contains lighting fixtures
#                    (the one used last time if there are several)
#     * 2D family  : one family selected by the user
#     * types      : each linked "Family : Type" is matched to the closest
#                    existing type name (spaces, dashes, case and word order
#                    ignored); no match -> a new type in the selected family
#     * position   : linked fixture insertion point, transformed to host coords
#     * updates    : optionally change a different symbol already at the target
#                    position to the type matching the linked fixture
#     * wattage    : read from each linked fixture (instance or type) and
#                    written to the placed 2D symbol (same parameter name, or
#                    Wattage / Power / Apparent Load ...), units converted
#     * preview    : a window with a 3D preview, the type matching (with
#                    wattage) and a summary - nothing changes until you press
#                    "Place symbols"
#   Shift+Click      -> choose the link again if several are loaded
#
# The output window lists every linked type and the 2D type it was matched to.
# One transaction: a single Ctrl+Z undoes everything.
# Model families are placed at each fixture's EXACT elevation: after placing or
# moving, the symbol's real height is measured and corrected (offset parameter,
# then vertical move) to within 1 mm. View-specific families
# are placed in a plan view assigned to each selected level.
# Revit 2023 - 2027, pyRevit (IronPython engine).
# =============================================================================

import math

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    ElementId,
    Family,
    FamilyInstance,
    FamilyPlacementType,
    FailureProcessingResult,
    FailureSeverity,
    FilteredElementCollector,
    IFailuresPreprocessor,
    Level,
    Line,
    LocationPoint,
    RevitLinkInstance,
    StorageType,
    SubTransaction,
    Transaction,
    ViewPlan,
    XYZ,
    ElementTransformUtils,
    SpecTypeId,
    UnitTypeId,
    UnitUtils,
)
from Autodesk.Revit.DB.Structure import StructuralType
from Autodesk.Revit.DB.Electrical import ElectricalCircuitPathMode, ElectricalSystem
from pyrevit import EXEC_PARAMS, forms, script


uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
active_view = doc.ActiveView
output = script.get_output()
config = script.get_config()
choose_again = EXEC_PARAMS.config_mode   # True on Shift+Click

DUPLICATE_TOLERANCE = 50.0 / 304.8          # 50 mm in feet
ALIGN_TOLERANCE = 1.0 / 304.8               # 1 mm in feet
ROTATION_TOLERANCE = math.radians(0.1)
INVALID_NAME_CHARS = u"\\:{}[]|;<>?`~"
TARGET_CATEGORIES = [
    BuiltInCategory.OST_LightingFixtures,
    BuiltInCategory.OST_DetailComponents,
    BuiltInCategory.OST_GenericAnnotation,
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def id_value(element_id):
    """ElementId.Value from Revit 2024, IntegerValue before (removed in 2026)."""
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def type_name(symbol):
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME):
        parameter = symbol.get_Parameter(bip)
        if parameter:
            value = parameter.AsString() or parameter.AsValueString()
            if value:
                return value
    return "Type {}".format(id_value(symbol.Id))


def clean_name(text):
    result = u"".join(u"-" if c in INVALID_NAME_CHARS else c for c in (text or u"")).strip()
    return result or u"Type"


def same(a, b):
    return (a or u"").strip().lower() == (b or u"").strip().lower()


def unique_name(wanted, used_lower):
    name = wanted
    suffix = 2
    while name.lower() in used_lower:
        name = u"{} ({})".format(wanted, suffix)
        suffix += 1
    return name


def level_for(z, levels):
    """Highest level at or below z (the fixture's own floor); else the lowest."""
    if not levels:
        return None
    below = [level for level in levels if level.Elevation <= z + 1e-6]
    return below[-1] if below else levels[0]


def fixture_level(fixture, link_doc, link_levels, source_z):
    """Prefer the linked instance's assigned level; infer from elevation only
    for families without a usable level association."""
    level_ids = []
    try:
        level_ids.append(fixture.LevelId)
    except Exception:
        pass
    for parameter_id in (
            BuiltInParameter.FAMILY_LEVEL_PARAM,
            BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM,
            BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM):
        try:
            parameter = fixture.get_Parameter(parameter_id)
            if parameter and parameter.StorageType == StorageType.ElementId:
                level_ids.append(parameter.AsElementId())
        except Exception:
            pass

    for level_id in level_ids:
        if level_id is None or level_id == ElementId.InvalidElementId:
            continue
        try:
            level = link_doc.GetElement(level_id)
            if isinstance(level, Level):
                return level
        except Exception:
            pass
    return level_for(source_z, link_levels) if source_z is not None else None


def host_level_for(link_level, link_transform, host_levels):
    """Match a linked level by name, then by its transformed elevation."""
    if link_level is None or not host_levels:
        return None
    host_elevation = link_transform.OfPoint(
        XYZ(0.0, 0.0, link_level.Elevation)
    ).Z
    same_name = [level for level in host_levels
                 if same(level.Name, link_level.Name)]
    candidates = same_name or host_levels
    return min(candidates, key=lambda level: abs(level.Elevation - host_elevation))


def point_distance(a, b, plan_only):
    dz = 0.0 if plan_only else a.Z - b.Z
    return math.sqrt((a.X - b.X) ** 2 + (a.Y - b.Y) ** 2 + dz ** 2)


def rotation_difference(target, current):
    return math.atan2(math.sin(target - current), math.cos(target - current))


def source_reference_from_comments(instance):
    """Return (link instance id, linked fixture id) for symbols placed here."""
    try:
        parameter = instance.get_Parameter(
            BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
        )
        comments = parameter.AsString() if parameter else None
    except Exception:
        comments = None
    if not comments or not comments.startswith(linked_doc.Title + " |"):
        return None, None

    fixture_match = re.search(r"\|\s*ID\s+(-?\d+)(?:\s*\||$)", comments)
    if not fixture_match:
        return None, None
    link_match = re.search(r"\|\s*Link instance\s+(-?\d+)\s*\|", comments)
    source_link_id = int(link_match.group(1)) if link_match else None
    source_fixture_id = int(fixture_match.group(1))
    return source_link_id, source_fixture_id


def target_point(item, view_based):
    if view_based and item.target_level is not None:
        return XYZ(item.point.X, item.point.Y, item.target_level.Elevation)
    return item.point


def plan_view_for_level(view_id, level):
    if view_id is None or view_id == ElementId.InvalidElementId or level is None:
        return None
    try:
        view = doc.GetElement(view_id)
        if isinstance(view, ViewPlan) and view.GenLevel is not None \
                and view.GenLevel.Id == level.Id:
            return view
    except Exception:
        pass
    return None


def align_instance(instance, item, view_based):
    """Move and rotate an existing symbol to the linked fixture location."""
    location = instance.Location
    if not isinstance(location, LocationPoint):
        raise Exception("Existing symbol has no point location.")

    destination = target_point(item, view_based)
    if point_distance(location.Point, destination, view_based) > ALIGN_TOLERANCE:
        ElementTransformUtils.MoveElement(
            doc, instance.Id, destination - location.Point
        )
        location = instance.Location

    if rotate and abs(item.rotation) > 1e-6 and not is_face_hosted(instance):
        rotation_delta = rotation_difference(item.rotation, location.Rotation)
        if abs(rotation_delta) > ROTATION_TOLERANCE:
            origin = location.Point
            ElementTransformUtils.RotateElement(
                doc, instance.Id,
                Line.CreateBound(origin, origin + XYZ.BasisZ),
                rotation_delta
            )
    check_elevation(instance, item)


def placement_of(fixture, transform, use_centre):
    """Host point (centre or insertion point) and rotation of a linked fixture."""
    location = fixture.Location
    insertion = location.Point if isinstance(location, LocationPoint) else None
    source = insertion
    if use_centre or source is None:
        box = fixture.get_BoundingBox(None)
        if box:
            centre = (box.Min + box.Max) * 0.5
            # keep the fixture's own height; only X / Y move to the centre
            source = centre if insertion is None else XYZ(centre.X, centre.Y, insertion.Z)
    if source is None:
        return None, 0.0
    rotation = 0.0
    try:
        hand = transform.OfVector(fixture.HandOrientation)
        if hand.GetLength() > 1e-9:
            rotation = math.atan2(hand.Y, hand.X)
    except Exception:
        pass
    return transform.OfPoint(source), rotation


ELEVATION_TOLERANCE = 1.0 / 304.8           # 1 mm in feet
OFFSET_PARAMETERS = (
    BuiltInParameter.INSTANCE_ELEVATION_PARAM,        # Elevation from Level
    BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM,  # Offset from Host
)
elevation_problems = []    # [linked type, linked id, expected mm, actual mm]


def instance_z(instance):
    location = instance.Location
    return location.Point.Z if isinstance(location, LocationPoint) else None


def fix_elevation(instance, target_z):
    """Make the symbol's REAL insertion point sit at target_z (host, internal
    feet). Revit treats point Z differently per family type (level based,
    work-plane based, unhosted), so the result is measured after
    regeneration and corrected: first through the offset parameter, then by
    moving the element vertically. Returns the remaining error in feet."""
    doc.Regenerate()
    current = instance_z(instance)
    if current is None:
        return None
    error = target_z - current
    if abs(error) <= ELEVATION_TOLERANCE:
        return 0.0

    # 1) adjust the offset parameter by the measured error
    for bip in OFFSET_PARAMETERS:
        parameter = instance.get_Parameter(bip)
        if parameter is None or parameter.IsReadOnly or parameter.StorageType != StorageType.Double:
            continue
        try:
            parameter.Set(parameter.AsDouble() + error)
            doc.Regenerate()
            current = instance_z(instance)
            error = target_z - current
            if abs(error) <= ELEVATION_TOLERANCE:
                return 0.0
        except Exception:
            pass

    # 2) move the element vertically by whatever is left
    try:
        ElementTransformUtils.MoveElement(doc, instance.Id, XYZ(0.0, 0.0, error))
        doc.Regenerate()
        current = instance_z(instance)
        error = target_z - current
    except Exception:
        pass
    return 0.0 if abs(error) <= ELEVATION_TOLERANCE else error


def check_elevation(instance, item):
    """Model families only: correct and record any elevation mismatch."""
    if view_based:
        return
    remaining = fix_elevation(instance, item.point.Z)
    if remaining:
        actual = instance_z(instance)
        elevation_problems.append([
            u"{} : {}".format(*item.key), id_value(item.fixture.Id),
            int(round(item.point.Z * 304.8)),
            int(round(actual * 304.8)) if actual is not None else "-",
        ])


def same_floor(instance, point, item):
    """Plan-distance matching must not pick a symbol on another floor."""
    if view_based:
        return True
    try:
        if item.target_level is not None and instance.LevelId == item.target_level.Id:
            return True
    except Exception:
        pass
    z = instance_z(instance)
    return z is not None and abs(z - point.Z) < 1500.0 / 304.8


def is_face_hosted(instance):
    try:
        return instance.HostFace is not None
    except Exception:
        return False


def place_in_model(link, transform, fixture, symbol, point, levels,
                   assigned_level=None):
    errors = []
    level = assigned_level or level_for(point.Z, levels)

    # a) unhosted / level-based / work-plane-based families
    if level and symbol.Family.FamilyPlacementType != FamilyPlacementType.OneLevelBasedHosted:
        try:
            # the real elevation is measured and corrected afterwards (fix_elevation)
            return doc.Create.NewFamilyInstance(point, symbol, level, StructuralType.NonStructural)
        except Exception as error:
            errors.append(str(error))

    # b) face-based families: host on the same linked face as the fixture
    try:
        host_face = fixture.HostFace
        if host_face:
            link_face = host_face.CreateLinkReference(link)
            direction = transform.OfVector(fixture.HandOrientation)
            if direction.GetLength() > 1e-9:
                return doc.Create.NewFamilyInstance(link_face, point, direction.Normalize(), symbol)
    except Exception as error:
        errors.append(str(error))

    # c) last resort: free placement
    try:
        return doc.Create.NewFamilyInstance(point, symbol, StructuralType.NonStructural)
    except Exception as error:
        errors.append(str(error))

    raise Exception("could not be placed ({}). Use a non-hosted 2D family.".format(
        " / ".join(sorted(set(errors)))))


def place_in_view(symbol, point, target_view, target_level):
    elevation = target_level.Elevation if target_level is not None else point.Z
    return doc.Create.NewFamilyInstance(
        XYZ(point.X, point.Y, elevation), symbol, target_view
    )


class WarningSwallower(IFailuresPreprocessor):
    """Deletes warnings (e.g. 'identical instances') so no popup per element."""
    def PreprocessFailures(self, failures_accessor):
        for failure in failures_accessor.GetFailureMessages():
            if failure.GetSeverity() == FailureSeverity.Warning:
                failures_accessor.DeleteWarning(failure)
        return FailureProcessingResult.Continue


class Option(object):
    """List item for forms.SelectFromList (shows .name)."""
    def __init__(self, name, value=None):
        self.name = name
        self.value = value

    def __str__(self):
        return self.name


# -----------------------------------------------------------------------------
# Name matching (automatic)
# -----------------------------------------------------------------------------
import re

STOP_WORDS = set(["type", "family", "light", "lighting", "fixture", "fixtures", "luminaire", "2d", "3d"])


def norm(text):
    """'Downlight_600 x 600 - LED' -> 'downlight600x600led'"""
    return re.sub(r"[^0-9a-z]", "", (text or u"").lower())


def tokens(text):
    words = re.findall(r"[0-9a-z]+", (text or u"").lower())
    return set(w for w in words if w not in STOP_WORDS)


def match_score(target_name, linked_family, linked_type):
    """0 - 100: how well a 2D type name fits a linked Family : Type."""
    t = norm(target_name)
    if not t:
        return 0
    lt = norm(linked_type)
    lf = norm(linked_family)
    if t == lt:
        return 100
    if t == lf + lt or t == lt + lf:
        return 98
    if t == lf:
        return 85
    best = 0
    # one name contains the other (e.g. "DL01" in "DL01 - Downlight 600")
    for source in (lt, lf + lt):
        if len(t) >= 3 and len(source) >= 3 and (t in source or source in t):
            ratio = float(min(len(t), len(source))) / max(len(t), len(source))
            best = max(best, int(60 + 30 * ratio))
    # shared words, any order (e.g. "LED Panel 600" vs "600 Panel LED")
    target_words = tokens(target_name)
    linked_words = tokens(linked_type) | tokens(linked_family)
    if target_words and linked_words:
        common = target_words & linked_words
        jaccard = float(len(common)) / len(target_words | linked_words)
        coverage = float(len(common)) / len(target_words)
        best = max(best, int(45 * jaccard + 35 * coverage))
    return best


AUTO_MATCH = 60   # score needed to reuse an existing 2D type


def best_symbol(symbols, key):
    best, best_score = None, 0
    for symbol in symbols:
        score = match_score(type_name(symbol), key[0], key[1])
        if score > best_score:
            best, best_score = symbol, score
    return (best, best_score) if best_score >= AUTO_MATCH else (None, best_score)


def collect_groups(link_doc):
    result = {}
    for fixture in FilteredElementCollector(link_doc) \
            .OfCategory(BuiltInCategory.OST_LightingFixtures) \
            .OfClass(FamilyInstance):
        if fixture.Symbol is None or fixture.SuperComponent is not None:
            continue
        key = (fixture.Symbol.FamilyName, type_name(fixture.Symbol))
        result.setdefault(key, []).append(fixture)
    return result


# -----------------------------------------------------------------------------
# 1. Link (automatic)
# -----------------------------------------------------------------------------
candidates = []
for link_instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
    try:
        link_doc = link_instance.GetLinkDocument()
    except Exception:
        link_doc = None
    if link_doc is None:
        continue
    found = collect_groups(link_doc)
    if found:
        candidates.append((link_instance, link_doc, found))
if not candidates:
    forms.alert("No loaded Revit link contains lighting fixtures.\n"
                "Load the lighting link first (Manage > Manage Links).", exitscript=True)


def link_label(item):
    return u"{}   ({} fixtures)".format(item[1].Title, sum(len(v) for v in item[2].values()))


chosen = None
if len(candidates) == 1:
    chosen = candidates[0]
else:
    remembered = getattr(config, "link_title", None)
    if remembered and not choose_again:
        chosen = next((c for c in candidates if c[1].Title == remembered), None)
    if chosen is None:
        picked = forms.SelectFromList.show(
            [Option(link_label(c), c) for c in candidates], name_attr="name",
            title="Several links have lighting fixtures - pick one (remembered next time)",
            button_name="Use this link")
        if not picked:
            script.exit()
        chosen = picked.value
link, linked_doc, groups = chosen
transform = link.GetTotalTransform()
config.link_title = linked_doc.Title
group_keys = sorted(groups.keys(), key=lambda k: (k[0].lower(), k[1].lower()))


# -----------------------------------------------------------------------------
# 2. 2D family (chosen by the user; new types stay in this family)
# -----------------------------------------------------------------------------
category_ids = set(int(c) for c in TARGET_CATEGORIES)
families = []
for family in FilteredElementCollector(doc).OfClass(Family):
    try:
        category = family.FamilyCategory
    except Exception:
        continue
    if category is None or id_value(category.Id) not in category_ids:
        continue
    symbols = [doc.GetElement(i) for i in family.GetFamilySymbolIds()]
    symbols = sorted([s for s in symbols if s], key=lambda s: type_name(s).lower())
    if symbols:
        families.append((family, symbols, category.Name))
if not families:
    forms.alert("No Lighting Fixture, Detail Item or Generic Annotation families are loaded.\n"
                "Load your family first (Insert > Load Family).", exitscript=True)

linked_family_names = set(norm(k[0]) for k in group_keys)


def family_fit(item):
    family, symbols, _ = item
    if norm(family.Name) in linked_family_names:
        return -1        # that is the linked 3D family itself, not a 2D symbol family
    matched = sum(1 for key in group_keys if best_symbol(symbols, key)[0] is not None)
    bonus = 0.5 if "2d" in family.Name.lower() or "symbol" in family.Name.lower() else 0
    return matched + bonus


ranked = sorted(families, key=family_fit, reverse=True)
picked = forms.SelectFromList.show(
    [Option(u"{}:  {}".format(f[2], f[0].Name), f) for f in ranked],
    name_attr="name",
    title="Choose ONE family. Missing linked types will be created in this family.",
    button_name="Use this family")
if not picked:
    script.exit()
chosen_family = picked.value
family, family_symbols, _ = chosen_family

view_based = family.FamilyPlacementType == FamilyPlacementType.ViewBased
base_symbol = family_symbols[0]


# -----------------------------------------------------------------------------
# 3. Type matching (automatic)
# -----------------------------------------------------------------------------
type_to_families = {}
for fam_name, typ_name in group_keys:
    type_to_families.setdefault(norm(typ_name), set()).add(fam_name)

mapping = {}       # key -> ("existing", symbol) | ("new", name)
match_rows = []
for key in group_keys:
    fam_name, typ_name = key
    symbol, score = best_symbol(family_symbols, key)
    if symbol is not None:
        mapping[key] = ("existing", symbol)
        match_rows.append([u"{} : {}".format(fam_name, typ_name), len(groups[key]),
                           type_name(symbol), "existing ({}%)".format(score)])
    else:
        shared = len(type_to_families[norm(typ_name)]) > 1
        new_name = clean_name(u"{} - {}".format(fam_name, typ_name) if shared else typ_name)
        mapping[key] = ("new", new_name)
        match_rows.append([u"{} : {}".format(fam_name, typ_name), len(groups[key]),
                           new_name, "NEW type"])


# -----------------------------------------------------------------------------
# 4. Options (fixed defaults - no questions)
# -----------------------------------------------------------------------------
use_centre = False
rotate = True
skip_dupes = True
write_comments = True

# -----------------------------------------------------------------------------
# 5. Wattage (read from link, write to host)
# -----------------------------------------------------------------------------
WATT_NAMES = [
    "Wattage", "Lamp Wattage", "Wattage (W)", "Watts", "Power", "Rated Power",
    "Lamp Power", "Luminaire Wattage", "Electrical Load", "Load", "Apparent Load",
]
WATT_BUILTINS = [getattr(BuiltInParameter, name, None)
                 for name in ("RBS_ELEC_APPARENT_LOAD", "FBX_LIGHT_WATTAGE")]


def data_type(parameter):
    try:
        return parameter.Definition.GetDataType()
    except Exception:
        return None


def first_number(text):
    found = re.search(r"-?\d+(?:[.,]\d+)?", text or u"")
    return float(found.group(0).replace(",", ".")) if found else None


def read_watts(parameter):
    """(watts as float, display text) or (None, None)."""
    if parameter is None or not parameter.HasValue:
        return None, None
    text = parameter.AsValueString() or parameter.AsString() or u""
    storage = parameter.StorageType
    if storage == StorageType.Double:
        value = parameter.AsDouble()
        spec = data_type(parameter)
        try:
            if spec == SpecTypeId.ElectricalPower:
                value = UnitUtils.ConvertFromInternalUnits(value, UnitTypeId.Watts)
            elif spec == SpecTypeId.ApparentPower:
                value = UnitUtils.ConvertFromInternalUnits(value, UnitTypeId.VoltAmperes)
        except Exception:
            pass
    elif storage == StorageType.Integer:
        value = float(parameter.AsInteger())
    elif storage == StorageType.String:
        value = first_number(parameter.AsString())
    else:
        value = None
    if value is None or value <= 0:
        return None, None
    return value, (text or u"{:g} W".format(value))


def linked_wattage(fixture):
    """Looks on the instance first, then its type. -> (watts, text, parameter name)."""
    for element in (fixture, fixture.Symbol):
        if element is None:
            continue
        for name in WATT_NAMES:
            watts, text = read_watts(element.LookupParameter(name))
            if watts is not None:
                return watts, text, name
        for bip in WATT_BUILTINS:
            if bip is None:
                continue
            parameter = element.get_Parameter(bip)
            watts, text = read_watts(parameter)
            if watts is not None:
                return watts, text, parameter.Definition.Name
    return None, None, None


def target_wattage_parameter(instance, symbol, source_name):
    """Writable wattage parameter on the placed instance, else on its type."""
    names = ([source_name] if source_name else []) + [n for n in WATT_NAMES if n != source_name]
    for element, level in ((instance, "instance"), (symbol, "type")):
        if element is None:
            continue
        for name in names:
            parameter = element.LookupParameter(name)
            if parameter is not None and not parameter.IsReadOnly:
                return parameter, level
    return None, None


def write_watts(parameter, watts, text):
    storage = parameter.StorageType
    if storage == StorageType.Double:
        spec = data_type(parameter)
        value = watts
        if spec == SpecTypeId.ElectricalPower:
            value = UnitUtils.ConvertToInternalUnits(watts, UnitTypeId.Watts)
        elif spec == SpecTypeId.ApparentPower:
            value = UnitUtils.ConvertToInternalUnits(watts, UnitTypeId.VoltAmperes)
        return parameter.Set(value)
    if storage == StorageType.Integer:
        return parameter.Set(int(round(watts)))
    if storage == StorageType.String:
        return parameter.Set(text)
    return False


# -----------------------------------------------------------------------------
# 6. Analyse every linked fixture (nothing changes yet)
# -----------------------------------------------------------------------------
levels = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda level: level.Elevation)
link_levels = sorted(
    FilteredElementCollector(linked_doc).OfClass(Level),
    key=lambda level: level.Elevation
)
views_by_level = {}
for plan_view in FilteredElementCollector(doc).OfClass(ViewPlan):
    if plan_view.IsTemplate:
        continue
    try:
        plan_level = plan_view.GenLevel
    except Exception:
        plan_level = None
    if plan_level is not None:
        views_by_level.setdefault(id_value(plan_level.Id), []).append(plan_view)

for plan_views in views_by_level.values():
    plan_views.sort(key=lambda view: (
        0 if view.Id == active_view.Id else 1,
        view.Name.lower()
    ))


class FixtureItem(object):
    def __init__(self, key, fixture):
        self.key = key
        self.fixture = fixture
        self.point, self.rotation = placement_of(fixture, transform, use_centre)
        self.watts, self.watt_text, self.watt_name = linked_wattage(fixture)
        source_z = transform.Inverse.OfPoint(self.point).Z \
            if self.point is not None else None
        self.level = fixture_level(
            fixture, linked_doc, link_levels, source_z
        )
        self.target_level = host_level_for(self.level, transform, levels)
        self.target_view = None
        self.status = "place"
        self.existing_instance = None
        self.existing_id = None


items = []
for key in group_keys:
    for fixture in groups[key]:
        items.append(FixtureItem(key, fixture))

level_counts = {}
host_level_by_link_id = {}
for item in items:
    if item.level is not None:
        level_id = id_value(item.level.Id)
        level_counts[level_id] = level_counts.get(level_id, 0) + 1
        if item.target_level is not None:
            host_level_by_link_id[level_id] = item.target_level

if not level_counts:
    forms.alert(
        "No linked fixture levels could be identified. Confirm the linked "
        "model contains Levels and that the lighting fixtures have a level "
        "association.",
        exitscript=True
    )

level_options = [Option(
    u"All linked levels  ({} fixtures)".format(sum(level_counts.values())),
    "ALL"
)]
for level in link_levels:
    count = level_counts.get(id_value(level.Id), 0)
    if count:
        host_level = host_level_by_link_id.get(id_value(level.Id))
        host_name = host_level.Name if host_level is not None else "No host level"
        level_options.append(Option(
            u"{} -> {}  ({} fixtures)".format(level.Name, host_name, count),
            level
        ))

selected_levels = forms.SelectFromList.show(
    level_options,
    name_attr="name",
    multiselect=True,
    title="Select linked fixture levels (counts use the fixtures' assigned level)",
    button_name="Use selected levels"
)
if not selected_levels:
    script.exit()
selected_level_ids = set(level_counts.keys()) if any(
    option.value == "ALL" for option in selected_levels
) else set(id_value(option.value.Id) for option in selected_levels)
selected_level_names = sorted(
    u"{} -> {}".format(
        level.Name,
        host_level_by_link_id[id_value(level.Id)].Name
        if id_value(level.Id) in host_level_by_link_id else "No host level"
    )
    for level in link_levels
    if id_value(level.Id) in selected_level_ids
)

for item in items:
    if item.target_level is not None:
        item.target_view = next(
            iter(views_by_level.get(id_value(item.target_level.Id), [])), None
        )

# Existing instances from the chosen family, including instances in other
# plan views so each selected level can be updated in its own view.
existing = []
for instance in FilteredElementCollector(doc).OfClass(FamilyInstance):
    symbol = instance.Symbol
    if symbol and symbol.Family and symbol.Family.Id == family.Id \
            and isinstance(instance.Location, LocationPoint):
        try:
            owner_view_id = instance.OwnerViewId
        except Exception:
            owner_view_id = None
        source_link_id, source_fixture_id = source_reference_from_comments(instance)
        existing.append((
            type_name(symbol), instance.Location.Point, instance,
            owner_view_id, source_link_id, source_fixture_id
        ))

matched_existing_ids = set()
for item in items:
    if item.point is None:
        item.status = "noloc"
        continue
    if item.level is None:
        item.status = "no_level"
        continue
    if id_value(item.level.Id) not in selected_level_ids:
        item.status = "outside"
        continue
    if item.target_level is None:
        item.status = "no_host_level"
        continue
    if view_based and item.target_view is None:
        item.status = "no_view"
        continue

    if skip_dupes:
        kind, value = mapping[item.key]
        target_name = type_name(value) if kind == "existing" else value
        destination = target_point(item, view_based)
        match = None

        # Script-created symbols keep the linked element id in Comments, so
        # they can be found and realigned even if they drifted well away.
        for record in existing:
            name, point, instance, owner_view_id, source_link_id, source_fixture_id = record
            instance_id = id_value(instance.Id)
            if instance_id in matched_existing_ids \
                    or source_fixture_id != id_value(item.fixture.Id) \
                    or (source_link_id is not None
                        and source_link_id != id_value(link.Id)):
                continue
            if view_based:
                owner_view = plan_view_for_level(
                    owner_view_id, item.target_level
                )
                if owner_view is None:
                    continue
                item.target_view = owner_view
            match = record
            break

        # Prefer nearby untagged symbols of the chosen family, without reusing
        # one existing symbol for more than one linked fixture.
        if match is None:
            nearest_distance = DUPLICATE_TOLERANCE
            for record in existing:
                name, point, instance, owner_view_id, source_link_id, source_fixture_id = record
                if id_value(instance.Id) in matched_existing_ids \
                        or source_fixture_id is not None:
                    continue
                if view_based:
                    owner_view = plan_view_for_level(
                        owner_view_id, item.target_level
                    )
                    if owner_view is None:
                        continue
                elif not same_floor(instance, destination, item):
                    continue
                # plan distance: a symbol at the WRONG HEIGHT is still found
                # (and then corrected) instead of being duplicated
                distance = point_distance(point, destination, True)
                if distance < nearest_distance:
                    nearest_distance = distance
                    match = record
                    if view_based:
                        item.target_view = owner_view

        # Older/manual symbols may be displaced by more than the proximity
        # tolerance. If the type and level identify a clear candidate, reuse
        # the nearest unmatched symbol so it is realigned instead of duplicated.
        if match is None:
            nearest_distance = None
            for record in existing:
                name, point, instance, owner_view_id, source_link_id, source_fixture_id = record
                if id_value(instance.Id) in matched_existing_ids \
                        or source_fixture_id is not None \
                        or not same(name, target_name):
                    continue
                if view_based:
                    owner_view = plan_view_for_level(
                        owner_view_id, item.target_level
                    )
                    if owner_view is None:
                        continue
                elif not same_floor(instance, destination, item):
                    continue
                # plan distance: a symbol at the WRONG HEIGHT is still found
                # (and then corrected) instead of being duplicated
                distance = point_distance(point, destination, True)
                if nearest_distance is None or distance < nearest_distance:
                    nearest_distance = distance
                    match = record
                    if view_based:
                        item.target_view = owner_view

        if match is not None:
            name, point, instance = match[0], match[1], match[2]
            matched_existing_ids.add(id_value(instance.Id))
            alignment_error = point_distance(point, destination, view_based)   # 3D for model families
            instance_location = instance.Location
            rotation_error = (
                abs(rotation_difference(
                    item.rotation, instance_location.Rotation
                ))
                if rotate and isinstance(instance_location, LocationPoint)
                else 0.0
            )
            item.existing_instance = instance
            item.existing_id = instance.Id
            item.status = (
                "existing"
                if same(name, target_name)
                and alignment_error <= ALIGN_TOLERANCE
                and rotation_error <= ROTATION_TOLERANCE
                else "update"
            )

to_place = [i for i in items if i.status == "place"]
already = [i for i in items if i.status == "existing"]
updates = [i for i in items if i.status == "update"]
outside = [i for i in items if i.status == "outside"]
no_view = [i for i in items if i.status == "no_view"]
no_location = [i for i in items if i.status in ("noloc", "no_level")]
no_host_level = [i for i in items if i.status == "no_host_level"]
selected_items = [
    item for item in items
    if item.level is not None
    and id_value(item.level.Id) in selected_level_ids
]
selected_group_keys = sorted(
    set(item.key for item in selected_items),
    key=lambda key: (key[0].lower(), key[1].lower())
)
with_watts = [i for i in selected_items if i.watts is not None]

# does the 2D family have somewhere to write the wattage?
probe_name = next((i.watt_name for i in with_watts), None)
# probe on an instance of the 2D family if one exists (instance parameters), else on its type
probe_instance = next((i for i in FilteredElementCollector(doc).OfClass(FamilyInstance)
                       if i.Symbol and i.Symbol.Family and i.Symbol.Family.Id == family.Id), None)
probe_param, probe_level = target_wattage_parameter(probe_instance, base_symbol, probe_name)
watt_note = (u"Wattage found on {} of {} linked fixture(s) (parameter '{}').".format(
    len(with_watts), len(selected_items), probe_name) if with_watts else u"No wattage found on the selected linked fixtures.")


def watt_summary(key):
    values = sorted(set(
        i.watt_text for i in selected_items if i.key == key and i.watt_text
    ))
    return u", ".join(values) if values else u"-"


rows = []
for key in group_keys:
    kind, value = mapping[key]
    key_items = [i for i in selected_items if i.key == key]
    if not key_items:
        continue
    rows.append({
        "linked": u"{} : {}".format(*key),
        "qty": len(key_items),
        "target": type_name(value) if kind == "existing" else value,
        "match": u"existing ({}%)".format(best_symbol(family_symbols, key)[1]) if kind == "existing" else u"NEW type",
        "watts": watt_summary(key),
        "new": len([i for i in key_items if i.status == "place"]),
        "update": len([i for i in key_items if i.status == "update"]),
    })


# -----------------------------------------------------------------------------
# 7. Preview window (same style as Panel Distance: header, 3D preview, tabs)
# -----------------------------------------------------------------------------
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System.Windows import (Window, Thickness, GridLength, GridUnitType, FontWeights,
                            WindowStartupLocation, CornerRadius, TextWrapping,
                            HorizontalAlignment, VerticalAlignment)
from System.Windows.Controls import (DockPanel, Dock, StackPanel, TextBlock, Button, Border,
                                     TabControl, TabItem, ScrollViewer, ScrollBarVisibility,
                                     Grid as WpfGrid, ColumnDefinition, RowDefinition,
                                     Orientation, CheckBox, Viewport3D)
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import (SolidColorBrush, Brushes, Color, ColorConverter, FontFamily)
from System.Windows.Media.Media3D import (ModelVisual3D, Model3DGroup, GeometryModel3D,
                                          MeshGeometry3D, DiffuseMaterial, AmbientLight,
                                          DirectionalLight, PerspectiveCamera, Point3D, Vector3D)


def brush(hex_color):
    return SolidColorBrush(ColorConverter.ConvertFromString(hex_color))


def make_button(text, primary):
    button = Button()
    button.Content = text
    button.MinWidth = 140 if primary else 90
    button.Height = 34
    button.Padding = Thickness(12, 4, 12, 4)
    button.Margin = Thickness(6, 0, 0, 0)
    button.FontWeight = FontWeights.SemiBold
    button.Background = brush("#2563EB") if primary else Brushes.White
    button.Foreground = Brushes.White if primary else brush("#334155")
    button.BorderBrush = brush("#2563EB") if primary else brush("#CBD5E1")
    return button


def text_block(text, size=12, bold=False, color="#334155", margin=None):
    block = TextBlock()
    block.Text = text
    block.FontSize = size
    block.TextWrapping = TextWrapping.Wrap
    block.Foreground = brush(color)
    if bold:
        block.FontWeight = FontWeights.SemiBold
    if margin is not None:
        block.Margin = margin
    return block


STATUS_COLORS = {
    "new_type": Color.FromRgb(245, 158, 11),     # amber  - will be placed with a NEW type
    "existing_type": Color.FromRgb(34, 211, 238),  # cyan - will be placed with your type
    "existing": Color.FromRgb(100, 116, 139),     # grey  - already placed earlier
    "update": Color.FromRgb(139, 92, 246),        # violet - existing symbol will change type
    "outside": Color.FromRgb(239, 68, 68),        # red   - not selected or no plan view
}


def item_color_key(item):
    if item.status == "existing":
        return "existing"
    if item.status == "update":
        return "update"
    if item.status in ("outside", "noloc", "no_level", "no_host_level", "no_view"):
        return "outside"
    return "existing_type" if mapping[item.key][0] == "existing" else "new_type"


class PreviewWindow(object):
    def __init__(self):
        self.accepted = False
        self.copy_watts = True
        self.update_existing = bool(updates)
        self.center = Point3D(0, 0, 0)
        self.radius = 10.0
        self.preset = "top"
        self.zoom = 1.0

        window = Window()
        window.Title = "Light Placement 2D"
        window.Width = 1120
        window.Height = 760
        window.MinWidth = 900
        window.MinHeight = 600
        window.WindowStartupLocation = WindowStartupLocation.CenterOwner
        window.Background = brush("#F1F4F9")
        window.FontFamily = FontFamily("Segoe UI")
        try:
            WindowInteropHelper(window).Owner = __revit__.MainWindowHandle
        except Exception:
            pass
        self.window = window

        root = DockPanel()
        header = StackPanel()
        header.Background = brush("#1E3A8A")
        header.Children.Add(text_block("LIGHT PLACEMENT 2D", 20, True, "#FFFFFF", Thickness(18, 12, 18, 0)))
        header.Children.Add(text_block(
            u"Link: {}   |   2D family: {}   |   {} fixture(s): {} to place, {} already placed{}".format(
                linked_doc.Title, family.Name, len(selected_items), len(to_place), len(already),
                u", {} to update".format(len(updates)) if updates else u""),
            12, False, "#DBEAFE", Thickness(18, 0, 18, 12)))
        DockPanel.SetDock(header, Dock.Top)
        root.Children.Add(header)

        # footer
        footer = DockPanel()
        footer.Margin = Thickness(18, 12, 18, 12)
        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        buttons.HorizontalAlignment = HorizontalAlignment.Right
        self.watt_box = CheckBox()
        self.watt_box.Content = "Copy linked wattage"
        self.watt_box.IsChecked = bool(with_watts)
        self.watt_box.IsEnabled = bool(with_watts)
        self.watt_box.VerticalAlignment = VerticalAlignment.Center
        self.watt_box.Margin = Thickness(0, 0, 12, 0)
        self.update_box = CheckBox()
        self.update_box.Content = "Realign / update existing symbols"
        self.update_box.IsChecked = bool(updates)
        self.update_box.IsEnabled = bool(updates)
        self.update_box.VerticalAlignment = VerticalAlignment.Center
        self.update_box.Margin = Thickness(0, 0, 12, 0)
        cancel = make_button("Cancel", False)
        cancel.IsCancel = True
        cancel.Click += lambda sender, args: window.Close()
        place = make_button(
            u"Place {} symbol(s)".format(len(to_place))
            if to_place else
            (u"Update existing symbol(s)" if updates else "Update wattage only"),
            True
        )
        place.IsEnabled = bool(to_place) or bool(updates) or bool(already and with_watts)
        self.place_button = place
        self.update_box.Checked += self.on_update_option_changed
        self.update_box.Unchecked += self.on_update_option_changed
        place.Click += self.on_place
        buttons.Children.Add(self.watt_box)
        buttons.Children.Add(self.update_box)
        buttons.Children.Add(cancel)
        buttons.Children.Add(place)
        DockPanel.SetDock(buttons, Dock.Right)
        footer.Children.Add(buttons)
        banner = text_block(watt_note + (u"" if probe_param is not None or not with_watts else
                            u"  No wattage parameter found on the 2D family - add an instance parameter "
                            u"'Wattage' (Electrical Power or Text) to copy it."),
                            11, False, "#334155")
        banner.VerticalAlignment = VerticalAlignment.Center
        footer.Children.Add(banner)
        DockPanel.SetDock(footer, Dock.Bottom)
        root.Children.Add(footer)

        tabs = TabControl()
        tabs.Margin = Thickness(18, 12, 18, 0)
        tabs.Items.Add(self.tab("3D preview", self.build_preview()))
        tabs.Items.Add(self.tab(u"Type matching ({})".format(len(rows)), self.build_matching()))
        tabs.Items.Add(self.tab("Overview", self.build_overview()))
        root.Children.Add(tabs)
        window.Content = root
        window.Loaded += lambda sender, args: self.set_camera(self.preset)

    def tab(self, header, content):
        item = TabItem()
        item.Header = header
        item.Content = content
        return item

    # ---- 3D preview -------------------------------------------------------
    def build_preview(self):
        page = DockPanel()
        top = DockPanel()
        top.Margin = Thickness(0, 0, 0, 8)
        legend = StackPanel()
        legend.Orientation = Orientation.Horizontal
        for key, label in (("existing_type", "Your existing type"), ("new_type", "New type"),
                           ("update", "Different existing type"), ("existing", "Already placed"),
                           ("outside", "Cannot place / missing level")):
            swatch = Border()
            swatch.Width = 12
            swatch.Height = 12
            swatch.CornerRadius = CornerRadius(2)
            swatch.Background = SolidColorBrush(STATUS_COLORS[key])
            swatch.Margin = Thickness(0, 0, 4, 0)
            legend.Children.Add(swatch)
            legend.Children.Add(text_block(label, 11, False, "#334155", Thickness(0, 0, 14, 0)))
        cams = StackPanel()
        cams.Orientation = Orientation.Horizontal
        for name in ("Top", "Iso", "Front", "Side", "Zoom +", "Zoom -"):
            button = make_button(name, False)
            button.Height = 26
            button.MinWidth = 55
            button.Click += self.camera_handler(name)
            cams.Children.Add(button)
        DockPanel.SetDock(cams, Dock.Right)
        top.Children.Add(cams)
        top.Children.Add(legend)
        DockPanel.SetDock(top, Dock.Top)
        page.Children.Add(top)

        self.viewport = Viewport3D()
        self.viewport.ClipToBounds = True
        self.viewport.MouseWheel += self.on_wheel
        frame = Border()
        frame.Background = brush("#0F172A")
        frame.CornerRadius = CornerRadius(8)
        frame.Child = self.viewport
        page.Children.Add(frame)
        self.build_scene()
        return page

    def build_scene(self):
        points = [i.point for i in selected_items if i.point is not None]
        if not points:
            return
        min_x = min(p.X for p in points); max_x = max(p.X for p in points)
        min_y = min(p.Y for p in points); max_y = max(p.Y for p in points)
        min_z = min(p.Z for p in points); max_z = max(p.Z for p in points)
        self.center = Point3D((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)
        self.radius = max(1.0, math.sqrt((max_x - min_x) ** 2 + (max_y - min_y) ** 2 + (max_z - min_z) ** 2) / 2)
        half = max(self.radius * 0.006, 0.3)

        meshes = dict((key, MeshGeometry3D()) for key in STATUS_COLORS)
        for item in selected_items:
            if item.point is not None:
                self.add_cube(meshes[item_color_key(item)], item.point, half)
        scene = Model3DGroup()
        scene.Children.Add(AmbientLight(Color.FromRgb(170, 180, 200)))
        scene.Children.Add(DirectionalLight(Color.FromRgb(255, 255, 255), Vector3D(-1, -2, -3)))
        for key, mesh in meshes.items():
            if mesh.Positions.Count:
                material = DiffuseMaterial(SolidColorBrush(STATUS_COLORS[key]))
                model = GeometryModel3D(mesh, material)
                model.BackMaterial = material
                scene.Children.Add(model)
        visual = ModelVisual3D()
        visual.Content = scene
        self.viewport.Children.Add(visual)

    @staticmethod
    def add_cube(mesh, point, half):
        start = mesh.Positions.Count
        for dx in (-half, half):
            for dy in (-half, half):
                for dz in (-half * 0.4, half * 0.4):
                    mesh.Positions.Add(Point3D(point.X + dx, point.Y + dy, point.Z + dz))
        for a, b, c, d in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            for index in (a, b, c, a, c, d):
                mesh.TriangleIndices.Add(start + index)

    def camera_handler(self, name):
        def handler(sender, args):
            if name == "Zoom +":
                self.zoom = max(0.05, self.zoom * 0.7)
            elif name == "Zoom -":
                self.zoom = min(5.0, self.zoom / 0.7)
            else:
                self.zoom = 1.0
                self.preset = name.lower()
            self.set_camera(self.preset)
        return handler

    def on_wheel(self, sender, args):
        self.zoom = max(0.05, min(5.0, self.zoom * (0.85 if args.Delta > 0 else 1.0 / 0.85)))
        self.set_camera(self.preset)

    def set_camera(self, preset):
        directions = {
            "top": (Vector3D(0, 0, 1), Vector3D(0, 1, 0)),
            "front": (Vector3D(0, -1, 0), Vector3D(0, 0, 1)),
            "side": (Vector3D(-1, 0, 0), Vector3D(0, 0, 1)),
        }
        direction, up = directions.get(preset, (Vector3D(1, -1, 1), Vector3D(0, 0, 1)))
        direction.Normalize()
        distance = max(10.0, self.radius / math.tan(math.pi / 8) * 1.4) * self.zoom
        position = Point3D(self.center.X + direction.X * distance,
                           self.center.Y + direction.Y * distance,
                           self.center.Z + direction.Z * distance)
        look = Vector3D(self.center.X - position.X, self.center.Y - position.Y, self.center.Z - position.Z)
        camera = PerspectiveCamera(position, look, up, 45)
        camera.NearPlaneDistance = 0.1
        camera.FarPlaneDistance = distance * 4
        self.viewport.Camera = camera

    # ---- type matching table ---------------------------------------------
    def build_matching(self):
        grid = WpfGrid()
        grid.Margin = Thickness(10)
        widths = (2.2, 0.4, 1.8, 0.8, 1.0, 0.55, 0.65)
        for width in widths:
            column = ColumnDefinition()
            column.Width = GridLength(width, GridUnitType.Star)
            grid.ColumnDefinitions.Add(column)
        headers = ("Linked fixture (Family : Type)", "Qty", "Placed as 2D type",
                   "Match", "Wattage (link)", "To place", "To update")
        all_rows = [headers] + [(r["linked"], str(r["qty"]), r["target"],
                                 r["match"], r["watts"], str(r["new"]),
                                 str(r["update"]))
                                for r in rows]
        for row_index, values in enumerate(all_rows):
            definition = RowDefinition()
            definition.Height = GridLength.Auto
            grid.RowDefinitions.Add(definition)
            for column_index, value in enumerate(values):
                is_new = row_index > 0 and column_index == 3 and value == u"NEW type"
                block = text_block(value, 12, row_index == 0, "#B45309" if is_new else "#0F172A",
                                   Thickness(0, 3, 10, 3))
                WpfGrid.SetRow(block, row_index)
                WpfGrid.SetColumn(block, column_index)
                grid.Children.Add(block)
        scroll = ScrollViewer()
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        scroll.Content = grid
        frame = Border()
        frame.Background = Brushes.White
        frame.CornerRadius = CornerRadius(8)
        frame.Child = scroll
        return frame

    def build_overview(self):
        lines = [
            u"Link: {}".format(linked_doc.Title),
            u"Family: {}   ({})".format(family.Name, "view-specific: placed in a plan view for each level"
                                        if view_based else "model family: placed at fixture height"),
            u"Selected level(s): {}".format(u", ".join(selected_level_names)),
            u"Level counts use the linked fixtures' Revit level assignment; "
            u"the host level is shown after the arrow.",
            u"",
            u"{} linked lighting fixture(s) in {} type(s).".format(
                len(selected_items), len(rows)),
            u"{} will be placed now.".format(len(to_place)),
            u"{} symbols already match the type and location.".format(len(already)),
            u"{} use your existing types, {} new type(s) will be created.".format(
                sum(1 for k in selected_group_keys if mapping[k][0] == "existing"),
                sum(1 for k in selected_group_keys if mapping[k][0] == "new")),
        ]
        lines.append(u"{} existing symbol(s) can be realigned and/or changed to the matched type "
                     u"(includes symbols at the wrong elevation).".format(len(updates)))
        if not view_based:
            lines.append(u"Model symbols are set to the linked fixture's exact elevation; the real height is "
                         u"measured after placing and corrected (within 1 mm).")
        if not view_based and id_value(family.FamilyCategory.Id) == int(
                BuiltInCategory.OST_LightingFixtures):
            lines.append(
                u"Matched fixtures' circuits will be reset to automatic All Devices wire paths. "
                u"Circuit membership and panel assignment are preserved; manual wire paths are replaced."
            )
        if outside:
            lines.append(u"{} skipped: outside the selected levels.".format(len(outside)))
        if no_view:
            lines.append(u"{} skipped: no plan view is assigned to the matching level.".format(
                len(no_view)))
        if no_location:
            lines.append(u"{} skipped: no point location or linked level could be determined.".format(
                len(no_location)))
        if no_host_level:
            lines.append(u"{} skipped: no matching host level.".format(
                len(no_host_level)))
        lines += [u"", watt_note]
        if with_watts:
            lines.append(u"Wattage is written to: {}".format(
                u"'{}' ({} parameter)".format(probe_param.Definition.Name, probe_level) if probe_param is not None
                else u"the placed symbol's own 'Wattage' instance parameter if it has one."))
        lines += [u"", u"Choose the 2D family each run. Missing types are created in that family.",
                  u"One Ctrl+Z undoes everything."]
        block = text_block(u"\n".join(lines), 13, False, "#0F172A", Thickness(16))
        frame = Border()
        frame.Background = Brushes.White
        frame.CornerRadius = CornerRadius(8)
        frame.Child = block
        return frame

    def on_place(self, sender, args):
        self.accepted = True
        self.copy_watts = self.watt_box.IsChecked == True
        self.update_existing = self.update_box.IsChecked == True
        self.window.Close()

    def on_update_option_changed(self, sender, args):
        self.place_button.IsEnabled = (
            bool(to_place)
            or (bool(updates) and self.update_box.IsChecked == True)
            or bool(already and with_watts)
        )
        if to_place:
            self.place_button.Content = u"Place {} symbol(s)".format(len(to_place))
        elif updates and self.update_box.IsChecked == True:
            self.place_button.Content = "Update existing symbol(s)"
        else:
            self.place_button.Content = "Update wattage only"

    def show(self):
        self.window.ShowDialog()
        return self.accepted


preview = PreviewWindow()
if not preview.show():
    script.exit()
copy_watts = preview.copy_watts
to_update = updates if preview.update_existing else []


# -----------------------------------------------------------------------------
# 8. Place + copy wattage (one transaction)
# -----------------------------------------------------------------------------
placed = 0
updated = 0
watt_written = 0
watt_missing_target = 0
created_types = []
failed = []
placed_per_type = {}
type_watts = {}        # type-level wattage: symbol id -> set of values written
affected_circuit_ids = set()
routed_circuits = 0


def apply_watts(instance, item):
    global watt_written, watt_missing_target
    if not copy_watts or item.watts is None:
        return
    parameter, level = target_wattage_parameter(instance, instance.Symbol, item.watt_name)
    if parameter is None:
        watt_missing_target += 1
        return
    if write_watts(parameter, item.watts, item.watt_text):
        watt_written += 1
        if level == "type":
            type_watts.setdefault(type_name(instance.Symbol), set()).add(item.watt_text)


def write_source_comments(instance, item):
    if not write_comments:
        return
    comments = instance.get_Parameter(
        BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
    )
    if comments and not comments.IsReadOnly:
        source_label = u"{} : {}".format(*item.key)
        comments.Set(u"{} | Link instance {} | ID {} | {}".format(
            linked_doc.Title,
            id_value(link.Id),
            id_value(item.fixture.Id),
            source_label
        ))


def connected_circuit_ids(instance):
    mep_model = instance.MEPModel
    if mep_model is None:
        return set()
    return set(id_value(system.Id) for system in mep_model.GetElectricalSystems())


transaction = Transaction(doc, "Place 2D lighting symbols from link")
handling = transaction.GetFailureHandlingOptions()
handling.SetFailuresPreprocessor(WarningSwallower())
transaction.SetFailureHandlingOptions(handling)
transaction.Start()
try:
    used_lower = set(type_name(s).lower() for s in family_symbols)
    symbol_for = {}
    for key in group_keys:
        if not any(i.key == key and (
                i.status == "place" or
                (preview.update_existing and i.status == "update")
        ) for i in items):
            continue        # nothing new to place for this type
        kind, value = mapping[key]
        if kind == "existing":
            symbol_for[key] = value
            continue
        try:
            name = unique_name(value, used_lower)
            symbol_for[key] = base_symbol.Duplicate(name)
            used_lower.add(name.lower())
            created_types.append(name)
        except Exception as error:
            failed.append([u"{} : {}".format(*key), "", u"New type '{}' not created: {}".format(value, error)])

    for symbol in symbol_for.values():
        if not symbol.IsActive:
            symbol.Activate()
    doc.Regenerate()

    # refresh wattage on symbols placed in an earlier run
    for item in already:
        instance = doc.GetElement(item.existing_id)
        if instance is not None:
            try:
                affected_circuit_ids.update(connected_circuit_ids(instance))
                apply_watts(instance, item)
            except Exception as error:
                failed.append([u"{} : {}".format(*item.key), id_value(item.fixture.Id),
                               u"circuit lookup / wattage: {}".format(error)])

    for item in to_update:
        symbol = symbol_for.get(item.key)
        source_label = u"{} : {}".format(*item.key)
        if symbol is None:
            failed.append([source_label, id_value(item.fixture.Id), "No 2D type available for update"])
            continue
        sub = SubTransaction(doc)
        sub.Start()
        try:
            instance = doc.GetElement(item.existing_id)
            if instance is None:
                raise Exception("Existing symbol instance was not found.")
            previous_circuit_ids = connected_circuit_ids(instance)
            changed_id = instance.ChangeTypeId(symbol.Id)
            if changed_id is not None and changed_id != ElementId.InvalidElementId:
                instance = doc.GetElement(changed_id) or instance
            else:
                instance = doc.GetElement(item.existing_id) or instance
            if connected_circuit_ids(instance) != previous_circuit_ids:
                raise Exception(
                    "Changing this fixture would change its circuit membership; "
                    "the update was rolled back."
                )
            align_instance(instance, item, view_based)
            if connected_circuit_ids(instance) != previous_circuit_ids:
                raise Exception(
                    "Moving this fixture changed its circuit membership; "
                    "the update was rolled back."
                )
            write_source_comments(instance, item)
            apply_watts(instance, item)
            sub.Commit()
            affected_circuit_ids.update(previous_circuit_ids)
            updated += 1
            label = type_name(symbol)
            placed_per_type[label] = placed_per_type.get(label, 0) + 1
        except Exception as error:
            sub.RollBack()
            failed.append([source_label, id_value(item.fixture.Id), u"update: {}".format(error)])

    for item in to_place:
        symbol = symbol_for.get(item.key)
        source_label = u"{} : {}".format(*item.key)
        if symbol is None:
            failed.append([source_label, id_value(item.fixture.Id), "No 2D type available"])
            continue
        sub = SubTransaction(doc)
        sub.Start()
        try:
            if view_based:
                instance = place_in_view(
                    symbol, item.point, item.target_view, item.target_level
                )
            else:
                instance = place_in_model(
                    link, transform, item.fixture, symbol, item.point,
                    levels, item.target_level
                )
            location = instance.Location
            if rotate and abs(item.rotation) > 1e-6 and isinstance(location, LocationPoint) \
                    and not is_face_hosted(instance):
                origin = location.Point
                ElementTransformUtils.RotateElement(
                    doc, instance.Id, Line.CreateBound(origin, origin + XYZ.BasisZ), item.rotation)
            check_elevation(instance, item)
            write_source_comments(instance, item)
            apply_watts(instance, item)
            sub.Commit()
            placed += 1
            label = type_name(symbol)
            placed_per_type[label] = placed_per_type.get(label, 0) + 1
        except Exception as error:
            sub.RollBack()
            failed.append([source_label, id_value(item.fixture.Id), str(error)])

    if affected_circuit_ids:
        found_circuit_ids = set()
        for circuit in FilteredElementCollector(doc).OfClass(ElectricalSystem):
            circuit_id = id_value(circuit.Id)
            if circuit_id not in affected_circuit_ids:
                continue
            circuit.CircuitPathMode = ElectricalCircuitPathMode.AllDevices
            found_circuit_ids.add(circuit_id)
            routed_circuits += 1
        missing_circuits = affected_circuit_ids - found_circuit_ids
        if missing_circuits:
            raise Exception(
                "Could not find affected electrical circuit(s): {}".format(
                    u", ".join(str(circuit_id) for circuit_id in sorted(missing_circuits))
                )
            )
        doc.Regenerate()

    if placed == 0 and updated == 0 and routed_circuits == 0 \
            and not created_types and watt_written == 0:
        transaction.RollBack()
    else:
        transaction.Commit()
except Exception:
    if transaction.HasStarted() and not transaction.HasEnded():
        transaction.RollBack()
    raise


# -----------------------------------------------------------------------------
# 9. Report
# -----------------------------------------------------------------------------
conflicts = [[name, u", ".join(sorted(values))] for name, values in type_watts.items() if len(values) > 1]

output.print_md(u"**Link:** {}  |  **2D family:** {}".format(linked_doc.Title, family.Name))
output.print_table(table_data=[[r["linked"], r["qty"], r["target"], r["match"], r["watts"]] for r in rows],
                   columns=["Linked fixture (Family : Type)", "Qty", "2D type", "Match", "Wattage"],
                   title="Type matching")
if placed_per_type:
    output.print_table(table_data=[[n, c] for n, c in sorted(placed_per_type.items())],
                       columns=["2D type", "Placed / updated"], title="Placed per type")
if created_types:
    output.print_table(table_data=[[n] for n in created_types], columns=["New type"], title="New types created")
if conflicts:
    output.print_table(table_data=conflicts, columns=["2D type", "Different wattages written"],
                       title="Wattage is a TYPE parameter but the linked fixtures differ - make it an instance parameter")
if failed:
    output.print_table(table_data=failed, columns=["Linked type", "Linked ID", "Reason"], title="Not placed")
if elevation_problems:
    output.print_table(
        table_data=elevation_problems,
        columns=["Linked type", "Linked ID", "Linked elevation (mm)", "Host symbol elevation (mm)"],
        title="Elevation could not be matched (hosted family? use a non-hosted 2D family)")

summary = [
    u"{} symbol(s) placed.".format(placed),
    u"{} existing symbol(s) realigned and/or updated to the matched type.".format(updated),
    u"{} new type(s) created.".format(len(created_types)),
    u"{} already placed earlier (skipped).".format(len(already)),
]
if routed_circuits:
    summary.append(
        u"{} affected circuit(s) reset to automatic All Devices wire paths; "
        u"membership and panel assignment were not changed.".format(routed_circuits)
    )
elif not view_based and id_value(family.FamilyCategory.Id) == int(
        BuiltInCategory.OST_LightingFixtures):
    summary.append("No connected circuits were found on the matched fixtures; no wire paths were recalculated.")
if outside:
    summary.append(u"{} skipped: outside the selected levels.".format(len(outside)))
if no_view:
    summary.append(u"{} skipped: no plan view is assigned to the matching level.".format(
        len(no_view)))
if no_location:
    summary.append(u"{} skipped: no point location or linked level could be determined.".format(
        len(no_location)))
if no_host_level:
    summary.append(u"{} skipped: no matching host level.".format(
        len(no_host_level)))
summary.insert(0, u"Selected linked level(s): {}".format(
    u", ".join(selected_level_names)))
if copy_watts:
    summary.append(u"Wattage copied to {} symbol(s).".format(watt_written))
    if watt_missing_target:
        summary.append(u"{} symbol(s) have no wattage parameter - add an instance parameter "
                       u"'Wattage' to the 2D family.".format(watt_missing_target))
if not view_based:
    if elevation_problems:
        summary.append(u"{} symbol(s) could NOT be set to the linked elevation - see the output window."
                       .format(len(elevation_problems)))
    elif placed or updated:
        summary.append(u"Elevation checked: every placed / updated symbol is at the linked fixture's "
                       u"elevation (within 1 mm).")
if failed:
    summary.append(u"{} problem(s) - see the output window.".format(len(failed)))
forms.alert(u"\n".join(summary) + u"\n\nOne Ctrl+Z undoes everything.", title="Light Placement 2D")
