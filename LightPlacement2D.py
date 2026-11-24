# -*- coding: utf-8 -*-
__title__ = "LIGHT\nPLACEMENT 2D"
__doc__ = (
    "Choose a lighting link, the levels to process and one 2D annotation "
    "family. Existing matching types are reused and missing types are created "
    "inside that family. Existing symbols within 350 mm are aligned onto the "
    "linked fixture (moved only - circuits kept); new symbols are added only "
    "where none exists."
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
#     * existing   : if a symbol of the chosen 2D family (or one this tool placed)
#                    is within 350 mm of the linked fixture on the same floor,
#                    it counts as existing and is ALIGNED onto the linked
#                    fixture (moved only: no type change, no parameters; the
#                    move is undone if it would change the element's circuit).
#                    Only linked fixtures with no symbol nearby get a new one.
#     * orphans    : existing symbols on the selected levels with NO linked
#                    fixture within 350 mm can be deleted (tab "No linked
#                    fixture"; off by default, only the chosen 2D family unless
#                    widened, circuit-connected fixtures kept by default).
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
# then vertical move) to within 1 mm. Fixtures at ZERO elevation from their
# level get their symbol 10 mm above the level (MIN_SYMBOL_LIFT) - exactly on
# the level it fell outside the plan view range. View-specific families
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
from pyrevit import EXEC_PARAMS, forms, script


uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
active_view = doc.ActiveView
output = script.get_output()
config = script.get_config()
choose_again = EXEC_PARAMS.config_mode   # True on Shift+Click

DUPLICATE_TOLERANCE = 50.0 / 304.8          # 50 mm in feet
ALIGN_TOLERANCE = 1.0 / 304.8               # 1 mm in feet
LEVEL_TOLERANCE = 5.0 / 304.8               # 5 mm: "on the level" (absorbs link round-off)
# Fixtures whose elevation from level is zero (or a hair below, from the link
# transform round-off) produced symbols lying exactly ON / just UNDER the level
# plane - outside the plan view range, so they never showed up. Model symbols
# are therefore kept at least this far above their host level.
MIN_SYMBOL_LIFT = 10.0 / 304.8              # 10 mm in feet
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
    below = [level for level in levels if level.Elevation <= z + LEVEL_TOLERANCE]
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


def model_z(item):
    """Height a MODEL symbol is placed at: the linked fixture's elevation, but
    never on / below its host level (zero-elevation fixtures)."""
    z = item.point.Z
    if item.target_level is not None:
        z = max(z, item.target_level.Elevation + MIN_SYMBOL_LIFT)
    return z


def model_point(item):
    return XYZ(item.point.X, item.point.Y, model_z(item))


def height_above_level(item):
    """Linked fixture height above its host level in mm (None if unknown)."""
    if item.point is None or item.target_level is None:
        return None
    return (item.point.Z - item.target_level.Elevation) * 304.8


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


OFF_GEOMETRY_TOLERANCE = 50.0 / 304.8       # insertion point this far outside the geometry
off_geometry_ids = set()                    # fixtures placed at their geometry centre


def placement_of(fixture, transform, use_centre):
    """Host point and rotation of a linked fixture.
    Normally the insertion point. Some families (level / work-plane hosted
    surface and wall lights) have their insertion point AWAY from their
    geometry - the symbol then landed beside the fixture. When the insertion
    point lies outside the fixture's own plan extent, the geometry centre is
    used in plan (height stays the fixture's own)."""
    location = fixture.Location
    insertion = location.Point if isinstance(location, LocationPoint) else None
    source = insertion
    box = None
    try:
        box = fixture.get_BoundingBox(None)
    except Exception:
        box = None
    if box is not None:
        centre = (box.Min + box.Max) * 0.5
        t = OFF_GEOMETRY_TOLERANCE
        outside = insertion is not None and (
            insertion.X < box.Min.X - t or insertion.X > box.Max.X + t or
            insertion.Y < box.Min.Y - t or insertion.Y > box.Max.Y + t)
        if use_centre or insertion is None or outside:
            # keep the fixture's own height; only X / Y move to the centre
            source = centre if insertion is None else XYZ(centre.X, centre.Y, insertion.Z)
            if outside:
                off_geometry_ids.add(id_value(fixture.Id))
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
    remaining = fix_elevation(instance, model_z(item))
    if remaining:
        actual = instance_z(instance)
        elevation_problems.append([
            u"{} : {}".format(*item.key), id_value(item.fixture.Id),
            int(round(model_z(item) * 304.8)),
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


def is_tilted(instance):
    """True when the instance does not lie flat (hosted on a sloped / vertical
    face). Its 2D symbolic lines are then drawn in that tilted plane and plan
    views do NOT show them."""
    try:
        return abs(instance.GetTransform().BasisZ.Z) < 0.99
    except Exception:
        return False


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
        try:
            # a2) same, on the level plane itself; the height is corrected afterwards
            return doc.Create.NewFamilyInstance(XYZ(point.X, point.Y, level.Elevation), symbol,
                                                level, StructuralType.NonStructural)
        except Exception as error:
            errors.append(str(error))

    # b) face-based families: host on the same linked face as the fixture -
    #    ONLY when that face is horizontal (on a sloped / vertical face the
    #    symbol's 2D lines would not show in plan)
    try:
        host_face = fixture.HostFace
        if host_face and not is_tilted(fixture):
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
        try:
            source_z = fixture.Location.Point.Z      # link coordinates, no round-off
        except Exception:
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

# -----------------------------------------------------------------------------
# Existing symbols: a 2D symbol within 350 mm (plan, same floor) of a linked
# fixture is that fixture's symbol. It is ALIGNED onto the fixture in plan
# (X / Y only - height, type, parameters and circuits unchanged). Fixtures
# with no symbol within 350 mm (new or missing) get a new symbol.
# Counted as a symbol: every instance of the chosen 2D family, plus symbols
# this tool placed earlier (Comments) in any family. Other host lighting
# fixtures (3D fixtures, other families) are IGNORED - they used to block new
# symbols and could even be moved. A symbol covers only ONE linked fixture.
# -----------------------------------------------------------------------------
EXISTING_RADIUS = 350.0 / 304.8          # 350 mm in feet

existing = []        # (point, instance, owner view id, source link id, source fixture id)
tilted_records = []  # our symbols lying on a sloped / vertical face: invisible in plan
lighting_category = int(BuiltInCategory.OST_LightingFixtures)
for instance in FilteredElementCollector(doc).OfClass(FamilyInstance):
    try:
        symbol = instance.Symbol
        if symbol is None or not isinstance(instance.Location, LocationPoint):
            continue
        if instance.SuperComponent is not None:
            continue                       # nested part of another family
        in_family = symbol.Family is not None and symbol.Family.Id == family.Id
        source_link_id, source_fixture_id = source_reference_from_comments(instance)
        if not (in_family or source_fixture_id is not None):
            continue                       # not a 2D symbol of ours
        owner_view_id = instance.OwnerViewId
        record = (instance.Location.Point, instance, owner_view_id,
                  source_link_id, source_fixture_id)
        if (owner_view_id is None or owner_view_id == ElementId.InvalidElementId) \
                and is_tilted(instance):
            tilted_records.append(record)  # does not count: a flat symbol is placed
            continue
        existing.append(record)
    except Exception:
        continue


def on_item_floor(record, item):
    """True when the existing symbol belongs to the linked fixture's floor."""
    point, instance, owner_view_id = record[0], record[1], record[2]
    if owner_view_id is not None and owner_view_id != ElementId.InvalidElementId:
        # view-specific symbol (detail item / annotation): its plan view's level
        return plan_view_for_level(owner_view_id, item.target_level) is not None
    try:
        if instance.LevelId == item.target_level.Id:
            return True
    except Exception:
        pass
    return abs(point.Z - item.point.Z) < 1500.0 / 304.8


eligible = []
for item in items:
    if item.point is None:
        item.status = "noloc"
    elif item.level is None:
        item.status = "no_level"
    elif id_value(item.level.Id) not in selected_level_ids:
        item.status = "outside"
    elif item.target_level is None:
        item.status = "no_host_level"
    elif view_based and item.target_view is None:
        item.status = "no_view"
    else:
        eligible.append(item)

used_existing = set()

# 1) symbols this tool placed earlier carry the linked ID in Comments.
#    Copy-pasted symbols carry the SAME Comments, so the ID is only trusted
#    for the NEAREST such symbol, and only when it is close to the fixture
#    (otherwise copies far away were dragged across the plan onto it).
COMMENT_MATCH_RADIUS = 1500.0 / 304.8     # 1.5 m in feet
by_fixture_id = dict((id_value(i.fixture.Id), i) for i in eligible)
comment_candidates = {}
for record in existing:
    source_link_id, source_fixture_id = record[3], record[4]
    if source_fixture_id is None or (source_link_id is not None and source_link_id != id_value(link.Id)):
        continue
    item = by_fixture_id.get(source_fixture_id)
    if item is None:
        continue
    distance = point_distance(record[0], item.point, True)
    if distance <= COMMENT_MATCH_RADIUS and on_item_floor(record, item):
        comment_candidates.setdefault(source_fixture_id, []).append((distance, record))
for source_fixture_id, candidates in comment_candidates.items():
    item = by_fixture_id[source_fixture_id]
    distance, record = min(candidates, key=lambda c: c[0])
    item.existing_instance, item.existing_id = record[1], record[1].Id
    item.existing_distance = distance
    item.existing_how = u"Comments say it was placed for this fixture"
    item.status = "existing"
    used_existing.add(id_value(record[1].Id))

# 2) any other symbol within 350 mm (nearest pairs first, one-to-one)
cell = EXISTING_RADIUS
grid = {}
for record in existing:
    if id_value(record[1].Id) in used_existing:
        continue
    point = record[0]
    grid.setdefault((int(math.floor(point.X / cell)), int(math.floor(point.Y / cell))), []).append(record)

pairs = []
for item in eligible:
    if item.existing_id is not None:
        continue
    cx, cy = int(math.floor(item.point.X / cell)), int(math.floor(item.point.Y / cell))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for record in grid.get((cx + dx, cy + dy), []):
                distance = point_distance(record[0], item.point, True)
                if distance <= EXISTING_RADIUS and on_item_floor(record, item):
                    pairs.append((distance, item, record))
pairs.sort(key=lambda pair: pair[0])
for distance, item, record in pairs:
    instance_id = id_value(record[1].Id)
    if item.existing_id is not None or instance_id in used_existing:
        continue
    item.existing_instance, item.existing_id = record[1], record[1].Id
    item.existing_distance = distance
    item.existing_how = u"nearest symbol within 350 mm"
    item.status = "existing"
    used_existing.add(instance_id)

def connected_circuit_ids(instance):
    """Ids of the electrical circuits the element belongs to (empty for 2D symbols)."""
    try:
        model = instance.MEPModel
        systems = model.GetElectricalSystems() if model is not None else None
        return set(id_value(system.Id) for system in systems) if systems else set()
    except Exception:
        return set()


def is_view_specific(instance):
    try:
        owner = instance.OwnerViewId
        return owner is not None and owner != ElementId.InvalidElementId
    except Exception:
        return False


def align_target(item):
    """Where an EXISTING symbol must go: the linked fixture's X / Y only.
    Its height is never changed - moving existing symbols up to the fixture's
    height pushed them out of the plan view range and they vanished."""
    current = item.existing_instance.Location.Point
    return XYZ(item.point.X, item.point.Y, current.Z)


# Used only by the read-only check at the end (which symbols the open plan shows).
visible_in_view = None
view_level_id = None
try:
    if isinstance(active_view, ViewPlan) and active_view.GenLevel is not None:
        view_level_id = active_view.GenLevel.Id
        visible_in_view = True
except Exception:
    visible_in_view = None


# existing symbol within 350 mm but not exactly on the linked fixture -> "align"
for item in items:
    if item.status != "existing":
        continue
    try:
        current = item.existing_instance.Location.Point
        item.align_distance = point_distance(current, align_target(item), False)
        if item.align_distance > ALIGN_TOLERANCE:
            item.status = "align"
    except Exception:
        pass

# -----------------------------------------------------------------------------
# DIAGNOSE: linked fixtures picked before running (hover, Tab until the linked
# fixture highlights, click) are explained one by one at the end.
# -----------------------------------------------------------------------------
picked_refs = []
try:
    picked_refs = list(uidoc.Selection.GetReferences())   # Revit 2023+
except Exception:
    picked_refs = []
picked_ids = set()
picked_other_link = []
for reference in picked_refs:
    try:
        if reference.LinkedElementId == ElementId.InvalidElementId:
            continue
        if reference.ElementId != link.Id:
            other = doc.GetElement(reference.ElementId)
            picked_other_link.append(u"{} (ID {})".format(
                other.Name if other is not None else u"?", id_value(reference.LinkedElementId)))
            continue
        picked_ids.add(id_value(reference.LinkedElementId))
    except Exception:
        continue

STATUS_TEXT = {
    "outside": u"its linked level was not selected",
    "noloc": u"no insertion point",
    "no_level": u"no level found in the link",
    "no_host_level": u"no matching level in this model",
    "no_view": u"no plan view on the matching level",
}


def why(item):
    """Plain-language result for one linked fixture."""
    fid = id_value(item.fixture.Id)
    if item.status in STATUS_TEXT:
        return u"NOT placed: " + STATUS_TEXT[item.status]
    if item.status in ("existing", "align"):
        text = u"NOT placed: symbol {} already counts for it ({}, {} mm away in plan)".format(
            id_value(item.existing_id), getattr(item, "existing_how", u"?"),
            int(round(getattr(item, "existing_distance", 0.0) * 304.8)))
        if item.status == "align":
            text += u" - aligned" if any(r[1] == fid for r in globals().get("aligned", [])) else u" - NOT aligned (align box off or move failed)"
        return text
    for row in globals().get("failed", []):
        if row[1] == fid:
            return u"FAILED: " + row[2]
    if "failed" not in globals():
        return u"would be placed (press Place in the preview)"
    return u"placed"


def report_picked():
    if picked_ids or picked_other_link:
        rows_pick = []
        for item in items:
            if id_value(item.fixture.Id) in picked_ids:
                above = height_above_level(item)
                rows_pick.append([u"{} : {}".format(*item.key), id_value(item.fixture.Id),
                                  item.level.Name if item.level is not None else u"-",
                                  int(round(above)) if above is not None else u"-",
                                  why(item),
                                  output.linkify(item.existing_id) if item.existing_id is not None else u"-"])
        found = set(r[1] for r in rows_pick)
        for missing in sorted(picked_ids - found):
            rows_pick.append([u"-", missing, u"-", u"-",
                              u"NOT read: not a Lighting Fixture category element, or a nested family", u"-"])
        for text in picked_other_link:
            rows_pick.append([u"-", text, u"-", u"-",
                              u"NOT read: it is in ANOTHER link - Shift+Click the button to choose that link", u"-"])
        output.print_table(
            table_data=rows_pick,
            columns=["Linked type", "Linked ID", "Linked level", "Height above level (mm)", "Result", "Blocking symbol (click)"],
            title=u"Your selected linked fixture(s) - why")


to_place = [i for i in items if i.status == "place"]
already = [i for i in items if i.status == "existing"]
to_align = [i for i in items if i.status == "align"]

# -----------------------------------------------------------------------------
# ORPHANS: existing symbols on the selected levels that have NO linked fixture.
# Safety: a symbol is only an orphan when no linked fixture at all (any level,
# any status) lies within 350 mm of it in plan on the same floor.
# -----------------------------------------------------------------------------
selected_host_level_ids = set(id_value(i.target_level.Id) for i in items
                              if i.target_level is not None and i.level is not None
                              and id_value(i.level.Id) in selected_level_ids)


def record_level_id(record):
    point, instance, owner_view_id = record[0], record[1], record[2]
    if owner_view_id is not None and owner_view_id != ElementId.InvalidElementId:
        try:
            view = doc.GetElement(owner_view_id)
            if isinstance(view, ViewPlan) and view.GenLevel is not None:
                return id_value(view.GenLevel.Id)
        except Exception:
            pass
        return None
    try:
        level_id = instance.LevelId
        if level_id is not None and level_id != ElementId.InvalidElementId:
            return id_value(level_id)
    except Exception:
        pass
    level = level_for(point.Z, levels)
    return id_value(level.Id) if level is not None else None


linked_grid = {}
for item in items:
    if item.point is not None:
        linked_grid.setdefault((int(math.floor(item.point.X / EXISTING_RADIUS)),
                                int(math.floor(item.point.Y / EXISTING_RADIUS))), []).append(item.point)


def near_any_linked(point, ignore_height):
    cx, cy = int(math.floor(point.X / EXISTING_RADIUS)), int(math.floor(point.Y / EXISTING_RADIUS))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for linked_point in linked_grid.get((cx + dx, cy + dy), []):
                if point_distance(point, linked_point, True) <= EXISTING_RADIUS and \
                        (ignore_height or abs(point.Z - linked_point.Z) < 1500.0 / 304.8):
                    return True
    return False


class Orphan(object):
    def __init__(self, record):
        self.point = record[0]
        self.instance = record[1]
        self.id = record[1].Id
        symbol = record[1].Symbol
        self.label = u"{} : {}".format(symbol.FamilyName, type_name(symbol))
        self.in_family = symbol.Family is not None and symbol.Family.Id == family.Id
        self.circuits = connected_circuit_ids(record[1])


orphans = []
level_names = dict((id_value(level.Id), level.Name) for level in levels)
for record in existing:
    try:
        if id_value(record[1].Id) in used_existing:
            continue
        level_id = record_level_id(record)
        if level_id not in selected_host_level_ids:
            continue
        if near_any_linked(record[0], is_view_specific(record[1])):
            continue
        orphan = Orphan(record)
        orphan.level_name = level_names.get(level_id, u"-")
        orphan.level_id = level_id
        orphans.append(orphan)
    except Exception:
        continue
for record in tilted_records:
    try:
        level_id = record_level_id(record)
        if level_id not in selected_host_level_ids:
            continue
        orphan = Orphan(record)
        orphan.label += u"  (tilted - not shown in plan)"
        orphan.tilted = True
        orphan.level_name = level_names.get(level_id, u"-")
        orphan.level_id = level_id
        orphans.append(orphan)
    except Exception:
        continue


def orphans_to_delete(include_other, keep_circuited):
    return [o for o in orphans
            if (o.in_family or include_other) and not (keep_circuited and o.circuits)]
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
        "existing": u"{} / {}".format(len([i for i in key_items if i.status == "align"]),
                                      len([i for i in key_items if i.status == "existing"])),
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
                            HorizontalAlignment, VerticalAlignment, FrameworkElement,
                            FlowDirection, Rect)
from System.Windows import Point as WinPoint
from System.Windows.Controls import (DockPanel, Dock, StackPanel, TextBlock, Button, Border,
                                     TabControl, TabItem, ScrollViewer, ScrollBarVisibility,
                                     Grid as WpfGrid, ColumnDefinition, RowDefinition,
                                     Orientation, CheckBox, Viewport3D, ComboBox, ComboBoxItem,
                                     ListBox, ListBoxItem, TextBox, WrapPanel)
from System.Windows.Input import MouseButton, Cursors
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import (SolidColorBrush, Brushes, Color, ColorConverter, FontFamily,
                                  Pen, DashStyle, DoubleCollection, Typeface, FormattedText,
                                  LinearGradientBrush)
from System.Windows.Media.Media3D import (ModelVisual3D, Model3DGroup, GeometryModel3D,
                                          MeshGeometry3D, DiffuseMaterial, AmbientLight,
                                          DirectionalLight, OrthographicCamera, Point3D, Vector3D)
from System.Globalization import CultureInfo
from Autodesk.Revit.DB import Wall, LocationCurve
from Autodesk.Revit.DB import Grid as RvtGrid


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


# -----------------------------------------------------------------------------
# Preview: what happens to every linked fixture, drawn on the real building
# (walls of the loaded links, grids) so it can be checked without Revit.
# -----------------------------------------------------------------------------
PREVIEW_STATUSES = (
    ("place_type", u"New symbol - your type", (34, 197, 94)),
    ("place_new", u"New symbol - NEW type", (245, 158, 11)),
    ("align", u"Existing - will be aligned", (168, 85, 247)),
    ("ok", u"Existing - already on fixture", (148, 163, 184)),
    ("orphan", u"Existing - no linked fixture", (236, 72, 153)),
    ("skipped", u"Cannot place", (239, 68, 68)),
)
STATUS_RGB = dict((k, c) for k, _, c in PREVIEW_STATUSES)
STATUS_LABEL = dict((k, l) for k, l, _ in PREVIEW_STATUSES)
STATUS_SHORT = {"place_type": u"NEW", "place_new": u"NEW TYPE", "align": u"ALIGN",
                "ok": u"OK", "orphan": u"NO LINK", "skipped": u"SKIP"}
DRAW_ORDER = {"ok": 0, "orphan": 1, "align": 2, "place_type": 3, "place_new": 4, "skipped": 5}
LIST_ORDER = {"skipped": 0, "place_new": 1, "place_type": 2, "align": 3, "orphan": 4, "ok": 5}
SKIP_TEXT = {
    "noloc": u"no insertion point",
    "no_level": u"no level found in the link",
    "no_host_level": u"no matching level in this model",
    "no_view": u"no plan view on the matching level",
}
FEET_PER_M = 1000.0 / 304.8
WALL_CUT = 1.0 * FEET_PER_M          # walls are shown cut 1 m above the level, like a plan


def status_color(key, alpha=255):
    r, g, b = STATUS_RGB[key]
    return Color.FromArgb(alpha, r, g, b)


def frozen_brush(color):
    result = SolidColorBrush(color)
    result.Freeze()
    return result


def frozen_pen(color, thickness, dashes=None):
    pen = Pen(frozen_brush(color), thickness)
    if dashes:
        values = DoubleCollection()
        for value in dashes:
            values.Add(value)
        pen.DashStyle = DashStyle(values, 0.0)
    pen.Freeze()
    return pen


FILL = dict((key, frozen_brush(status_color(key))) for key in STATUS_RGB)
OUTLINE_PEN = frozen_pen(Color.FromArgb(235, 2, 6, 23), 1.2)
SELECT_PEN = frozen_pen(Color.FromRgb(255, 255, 255), 2.0)
SELECT_HALO = frozen_pen(Color.FromArgb(90, 255, 255, 255), 5.0)
ALIGN_PEN = frozen_pen(status_color("align"), 2.0)
ALIGN_RING = frozen_pen(status_color("align"), 2.0)
GRID_PEN = frozen_pen(Color.FromArgb(140, 248, 113, 113), 1.0, (10, 4, 2, 4))
GRID_BUBBLE_PEN = frozen_pen(Color.FromArgb(220, 248, 113, 113), 1.2)
ZONE_PEN = frozen_pen(Color.FromArgb(120, 203, 213, 225), 1.0, (3, 3))
SCALE_PEN = frozen_pen(Color.FromRgb(226, 232, 240), 2.0)
CARD_PEN = frozen_pen(Color.FromArgb(120, 148, 163, 184), 1.0)
CARD_FILL = frozen_brush(Color.FromArgb(240, 15, 23, 42))
BUBBLE_FILL = frozen_brush(Color.FromArgb(230, 15, 23, 42))
TONES = {
    "text": frozen_brush(Color.FromRgb(241, 245, 249)),
    "muted": frozen_brush(Color.FromRgb(148, 163, 184)),
    "label": frozen_brush(Color.FromRgb(203, 213, 225)),
    "grid": frozen_brush(Color.FromRgb(252, 165, 165)),
}
TONES.update(FILL)
TYPEFACE = Typeface("Segoe UI")
TYPEFACE_BOLD = Typeface("Segoe UI Semibold")
_text_cache = {}


def formatted(text, size=11.0, tone="text", bold=False):
    """Cached FormattedText for the overlay."""
    key = (text, size, tone, bold)
    value = _text_cache.get(key)
    if value is None:
        face = TYPEFACE_BOLD if bold else TYPEFACE
        try:
            value = FormattedText(text, CultureInfo.CurrentUICulture, FlowDirection.LeftToRight,
                                  face, size, TONES[tone], 1.0)
        except Exception:
            value = FormattedText(text, CultureInfo.CurrentUICulture, FlowDirection.LeftToRight,
                                  face, size, TONES[tone])
        if len(_text_cache) < 6000:
            _text_cache[key] = value
    return value


def preview_status(item):
    if item.status in SKIP_TEXT:
        return "skipped"
    if item.status == "align":
        return "align"
    if item.status == "existing":
        return "ok"
    return "place_type" if mapping[item.key][0] == "existing" else "place_new"


def planned_type_name(item):
    kind, value = mapping[item.key]
    return type_name(value) if kind == "existing" else value


class PreviewMarker(object):
    """One marker: a linked fixture (with what will happen to it) or a symbol with no fixture."""
    def __init__(self, kind, point, item=None, orphan=None, symbol_point=None, label=u""):
        self.kind = kind
        self.x, self.y, self.z = point.X, point.Y, point.Z
        self.item = item
        self.orphan = orphan
        self.sym = (symbol_point.X, symbol_point.Y, symbol_point.Z) if symbol_point is not None else None
        self.label = label or u""
        if item is not None:
            self.level_id = id_value(item.target_level.Id) if item.target_level is not None else None
            self.title = item.key[1]
            self.ident = id_value(item.fixture.Id)
            family_name = item.key[0]
        else:
            self.level_id = getattr(orphan, "level_id", None)
            self.title = orphan.label
            self.ident = id_value(orphan.id)
            family_name = u""
        self.search = u"{} {} {} {}".format(self.title, family_name, self.ident, self.label).lower()


def build_markers():
    markers = []
    for item in selected_items:
        if item.point is None:
            continue
        kind = preview_status(item)
        symbol_point = None
        label = planned_type_name(item) if kind in ("place_type", "place_new") else item.key[1]
        if kind in ("align", "ok") and item.existing_instance is not None:
            try:
                label = type_name(item.existing_instance.Symbol)
                if kind == "align":
                    symbol_point = item.existing_instance.Location.Point
            except Exception:
                pass
        markers.append(PreviewMarker(kind, item.point, item=item, symbol_point=symbol_point, label=label))
    for orphan in orphans:
        markers.append(PreviewMarker("orphan", orphan.point, orphan=orphan,
                                     label=orphan.label.split(u" : ")[-1]))
    return markers


def model_sources():
    """(document, transform or None) for the host and every loaded link."""
    sources = [(doc, None)]
    for link_instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_doc = link_instance.GetLinkDocument()
            if link_doc is not None:
                sources.append((link_doc, link_instance.GetTotalTransform()))
        except Exception:
            pass
    return sources


def scan_walls(bounds):
    """Wall footprints (host coordinates) near the fixtures:
    [(points [(x, y)], half width, z min, z max, (xmin, ymin, xmax, ymax))]."""
    xmin, ymin, xmax, ymax = bounds
    result = []
    scanned = 0
    for source_doc, tf in model_sources():
        try:
            walls = FilteredElementCollector(source_doc).OfClass(Wall)
        except Exception:
            continue
        for wall in walls:
            scanned += 1
            if scanned > 80000 or len(result) > 40000:
                return result
            try:
                location = wall.Location
                if not isinstance(location, LocationCurve):
                    continue
                box = wall.get_BoundingBox(None)
                if box is None:
                    continue
                points = list(location.Curve.Tessellate())
                if tf is not None:
                    points = [tf.OfPoint(p) for p in points]
                    z0, z1 = tf.OfPoint(box.Min).Z, tf.OfPoint(box.Max).Z
                else:
                    z0, z1 = box.Min.Z, box.Max.Z
                xs = [p.X for p in points]
                ys = [p.Y for p in points]
                if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
                    continue
                try:
                    half = max(wall.Width, 0.1) / 2.0
                except Exception:
                    half = 0.15
                result.append(([(p.X, p.Y) for p in points], half, min(z0, z1), max(z0, z1),
                               (min(xs), min(ys), max(xs), max(ys))))
            except Exception:
                continue
    return result


def scan_grids():
    """[(name, (x, y), (x, y))] of the host and linked grids (host coordinates)."""
    result = []
    seen = set()
    for source_doc, tf in model_sources():
        try:
            grids = FilteredElementCollector(source_doc).OfClass(RvtGrid)
        except Exception:
            continue
        for grid in grids:
            try:
                curve = grid.Curve
                a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
                if tf is not None:
                    a, b = tf.OfPoint(a), tf.OfPoint(b)
                key = (grid.Name, int(round(a.X)), int(round(a.Y)), int(round(b.X)), int(round(b.Y)))
                if key in seen:
                    continue
                seen.add(key)
                result.append((grid.Name, (a.X, a.Y), (b.X, b.Y)))
            except Exception:
                continue
    return result


def add_quad(mesh, a, b, c, d):
    start = mesh.Positions.Count
    for p in (a, b, c, d):
        mesh.Positions.Add(Point3D(p[0], p[1], p[2]))
    for index in (0, 1, 2, 0, 2, 3):
        mesh.TriangleIndices.Add(start + index)


def add_prism(mesh, p, q, half, z0, z1):
    """Wall segment p -> q (plan points) of half width `half`, from z0 to z1."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.sqrt(dx * dx + dy * dy)
    if length < 1e-6:
        return
    nx, ny = -dy / length * half, dx / length * half
    a = (p[0] + nx, p[1] + ny)
    b = (q[0] + nx, q[1] + ny)
    c = (q[0] - nx, q[1] - ny)
    d = (p[0] - nx, p[1] - ny)
    add_quad(mesh, (a[0], a[1], z1), (b[0], b[1], z1), (c[0], c[1], z1), (d[0], d[1], z1))
    for u, v in ((a, b), (b, c), (c, d), (d, a)):
        add_quad(mesh, (u[0], u[1], z0), (v[0], v[1], z0), (v[0], v[1], z1), (u[0], u[1], z1))


def add_slab(mesh, xmin, ymin, xmax, ymax, z0, z1):
    corners = ((xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax))
    add_quad(mesh, *[(x, y, z1) for x, y in corners])
    for i in range(4):
        u, v = corners[i], corners[(i + 1) % 4]
        add_quad(mesh, (u[0], u[1], z0), (v[0], v[1], z0), (v[0], v[1], z1), (u[0], u[1], z1))


def solid_model(mesh, color):
    material = DiffuseMaterial(frozen_brush(color))
    material.Freeze()
    mesh.Freeze()
    model = GeometryModel3D(mesh, material)
    model.BackMaterial = material
    return model


def bounds_of(markers, margin):
    xs = [m.x for m in markers]
    ys = [m.y for m in markers]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


class MarkerLayer(FrameworkElement):
    """Draws markers, align arrows, grids, labels and the scale bar on top of
    the 3D view (constant pixel size, always readable)."""
    owner = None

    def OnRender(self, drawing_context):
        if self.owner is not None:
            self.owner.render_overlay(drawing_context, self.ActualWidth, self.ActualHeight)


class PreviewWindow(object):
    def __init__(self):
        self.accepted = False
        self.copy_watts = True
        self.markers = build_markers()
        self.visible_kinds = set(STATUS_RGB.keys())
        self.level_key = "ALL"
        self.level_keys = []
        self.shown = []
        self.screen = []
        self.list_markers = []
        self.hover = None
        self.hover_pos = (0.0, 0.0)
        self.selected = None
        self.drag = None
        self.walls = None
        self.grids = None
        self.context_cache = {}
        self.current_context = None
        self.show_walls = True
        self.show_grids = True
        self.show_labels = True
        self.show_zones = False
        self.list_guard = False
        self.render_error = None
        self.fit_pending = True
        self.levels_by_id = dict((id_value(level.Id), level) for level in levels)
        self.target = (0.0, 0.0, 0.0)
        self.width = 100.0
        self.extent = 100.0
        if self.markers:
            box = bounds_of(self.markers, 0.0)
            zs = [m.z for m in self.markers]
            self.extent = max(box[2] - box[0], box[3] - box[1], max(zs) - min(zs), 30.0)
        # orientation of the open Revit plan, so the preview looks the same
        self.view_yaw = -math.pi / 2
        try:
            up = active_view.UpDirection
            if abs(up.Z) < 0.5:
                self.view_yaw = math.atan2(-up.Y, -up.X)
        except Exception:
            pass
        self.yaw = self.view_yaw
        self.pitch = math.pi / 2

        window = Window()
        window.Title = "Light Placement 2D"
        window.Width = 1360
        window.Height = 880
        window.MinWidth = 1000
        window.MinHeight = 640
        window.WindowStartupLocation = WindowStartupLocation.CenterOwner
        window.Background = brush("#F1F4F9")
        window.FontFamily = FontFamily("Segoe UI")
        try:
            WindowInteropHelper(window).Owner = __revit__.MainWindowHandle
        except Exception:
            pass
        self.window = window

        # orphan deletion options (shown on the "No linked fixture" tab)
        self.delete_box = CheckBox()
        self.delete_box.Content = u"Delete existing symbols that have NO linked fixture"
        self.delete_box.IsChecked = False          # destructive: off unless you tick it
        self.other_box = CheckBox()
        self.other_box.Content = u"Include symbols this tool placed in other families (not only '{}')".format(family.Name)
        self.other_box.IsChecked = False
        self.circuit_box = CheckBox()
        self.circuit_box.Content = u"Keep fixtures that are connected to a circuit (protects wiring)"
        self.circuit_box.IsChecked = True
        for box in (self.delete_box, self.other_box, self.circuit_box):
            box.Margin = Thickness(0, 4, 0, 4)
            box.Checked += lambda sender, args: self.refresh_button()
            box.Unchecked += lambda sender, args: self.refresh_button()
        self.orphan_count = text_block(u"", 12, True, "#BE185D", Thickness(0, 6, 0, 6))

        root = DockPanel()
        root.Children.Add(self.build_header())

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
        self.align_box = CheckBox()
        self.align_box.Content = u"Align existing within 350 mm ({})".format(len(to_align))
        self.align_box.IsChecked = bool(to_align)
        self.align_box.IsEnabled = bool(to_align)
        self.align_box.VerticalAlignment = VerticalAlignment.Center
        self.align_box.Margin = Thickness(0, 0, 12, 0)
        cancel = make_button("Cancel", False)
        cancel.IsCancel = True
        cancel.Click += lambda sender, args: window.Close()
        place = make_button(u"", True)
        self.place_button = place
        self.refresh_button()
        self.align_box.Checked += lambda sender, args: self.refresh_button()
        self.align_box.Unchecked += lambda sender, args: self.refresh_button()
        place.Click += self.on_place
        buttons.Children.Add(self.watt_box)
        buttons.Children.Add(self.align_box)
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
        tabs.Items.Add(self.tab("Preview", self.build_preview()))
        tabs.Items.Add(self.tab(u"Type matching ({})".format(len(rows)), self.build_matching()))
        tabs.Items.Add(self.tab(u"No linked fixture ({})".format(len(orphans)), self.build_orphans()))
        tabs.Items.Add(self.tab("Overview", self.build_overview()))
        root.Children.Add(tabs)
        window.Content = root
        window.Loaded += self.on_loaded

    def tab(self, header, content):
        item = TabItem()
        item.Header = header
        item.Content = content
        return item

    # ---- header with result cards ---------------------------------------
    def build_header(self):
        counts = {}
        for marker in self.markers:
            counts[marker.kind] = counts.get(marker.kind, 0) + 1
        header = StackPanel()
        gradient = LinearGradientBrush(ColorConverter.ConvertFromString("#0F172A"),
                                       ColorConverter.ConvertFromString("#1E3A8A"), 0.0)
        header.Background = gradient
        header.Children.Add(text_block("LIGHT PLACEMENT 2D", 20, True, "#FFFFFF", Thickness(18, 12, 18, 0)))
        header.Children.Add(text_block(
            u"Link: {}   |   2D family: {}   |   Level(s): {}".format(
                linked_doc.Title, family.Name, u", ".join(selected_level_names)),
            12, False, "#BFDBFE", Thickness(18, 2, 18, 8)))
        cards = WrapPanel()
        cards.Margin = Thickness(18, 0, 18, 12)
        for key, title in (("place_type", u"NEW SYMBOLS"), ("place_new", u"NEW SYMBOLS - NEW TYPE"),
                           ("align", u"TO ALIGN"), ("ok", u"ALREADY ON FIXTURE"),
                           ("orphan", u"NO LINKED FIXTURE"), ("skipped", u"CANNOT PLACE")):
            card = Border()
            card.CornerRadius = CornerRadius(8)
            card.Background = SolidColorBrush(Color.FromArgb(40, 255, 255, 255))
            card.BorderBrush = SolidColorBrush(status_color(key))
            card.BorderThickness = Thickness(0, 0, 0, 3)
            card.Padding = Thickness(14, 6, 14, 6)
            card.Margin = Thickness(0, 0, 10, 0)
            card.MinWidth = 120
            inner = StackPanel()
            inner.Children.Add(text_block(str(counts.get(key, 0)), 22, True, "#FFFFFF"))
            inner.Children.Add(text_block(title, 10, True, "#CBD5E1"))
            card.Child = inner
            cards.Children.Add(card)
        header.Children.Add(cards)
        DockPanel.SetDock(header, Dock.Top)
        return header

    # ---- preview page -----------------------------------------------------
    def build_preview(self):
        page = DockPanel()

        # toolbar: level, camera buttons, layer toggles
        bar = WrapPanel()
        bar.Margin = Thickness(0, 0, 0, 8)
        bar.Children.Add(text_block(u"Level", 12, True, "#334155", Thickness(0, 6, 8, 0)))
        self.level_combo = ComboBox()
        self.level_combo.MinWidth = 240
        self.level_combo.Height = 28
        self.fill_levels()
        self.level_combo.SelectionChanged += self.on_level_changed
        bar.Children.Add(self.level_combo)
        for name, handler in ((u"Plan", self.on_plan), (u"3D", self.on_iso), (u"Fit", self.on_fit)):
            button = make_button(name, False)
            button.Height = 28
            button.MinWidth = 64
            button.Click += handler
            bar.Children.Add(button)
        spacer = Border()
        spacer.Width = 18
        bar.Children.Add(spacer)
        for tag, label, state in (("walls", u"Walls", self.show_walls), ("grids", u"Grids", self.show_grids),
                                  ("labels", u"Type labels", self.show_labels),
                                  ("zones", u"350 mm zones", self.show_zones)):
            toggle = CheckBox()
            toggle.Content = label
            toggle.Tag = tag
            toggle.IsChecked = state
            toggle.Margin = Thickness(0, 6, 14, 0)
            toggle.Checked += self.on_toggle
            toggle.Unchecked += self.on_toggle
            bar.Children.Add(toggle)
        DockPanel.SetDock(bar, Dock.Top)
        page.Children.Add(bar)

        body = WpfGrid()
        left_column = ColumnDefinition()
        left_column.Width = GridLength(1, GridUnitType.Star)
        right_column = ColumnDefinition()
        right_column.Width = GridLength(340)
        body.ColumnDefinitions.Add(left_column)
        body.ColumnDefinitions.Add(right_column)

        # the view: 3D context (floor + walls) with the marker layer on top
        stage = WpfGrid()
        stage.Background = Brushes.Transparent
        stage.ClipToBounds = True
        self.stage = stage
        self.viewport = Viewport3D()
        self.viewport.IsHitTestVisible = False
        self.camera = OrthographicCamera()
        self.viewport.Camera = self.camera
        lights = Model3DGroup()
        lights.Children.Add(AmbientLight(Color.FromRgb(105, 110, 125)))
        lights.Children.Add(DirectionalLight(Color.FromRgb(210, 210, 220), Vector3D(-0.4, -0.6, -1.0)))
        lights.Children.Add(DirectionalLight(Color.FromRgb(70, 70, 85), Vector3D(0.6, 0.5, -0.4)))
        light_visual = ModelVisual3D()
        light_visual.Content = lights
        self.viewport.Children.Add(light_visual)
        stage.Children.Add(self.viewport)
        self.layer = MarkerLayer()
        self.layer.owner = self
        self.layer.IsHitTestVisible = False
        stage.Children.Add(self.layer)
        self.status_text = text_block(u"", 11, True, "#E2E8F0", Thickness(12, 10, 12, 0))
        self.status_text.IsHitTestVisible = False
        self.status_text.HorizontalAlignment = HorizontalAlignment.Left
        self.status_text.VerticalAlignment = VerticalAlignment.Top
        stage.Children.Add(self.status_text)
        hint = text_block(u"Drag: pan   |   Right-drag: orbit   |   Wheel: zoom   |   "
                          u"Click: details   |   Double-click: zoom to it", 11, False, "#94A3B8",
                          Thickness(12, 0, 12, 10))
        hint.IsHitTestVisible = False
        hint.HorizontalAlignment = HorizontalAlignment.Right
        hint.VerticalAlignment = VerticalAlignment.Bottom
        stage.Children.Add(hint)
        stage.MouseDown += self.on_mouse_down
        stage.MouseMove += self.on_mouse_move
        stage.MouseUp += self.on_mouse_up
        stage.MouseWheel += self.on_wheel
        stage.MouseLeave += self.on_mouse_leave
        stage.SizeChanged += self.on_size_changed
        frame = Border()
        frame.CornerRadius = CornerRadius(10)
        frame.Background = LinearGradientBrush(ColorConverter.ConvertFromString("#0B1120"),
                                               ColorConverter.ConvertFromString("#1E293B"), 90.0)
        frame.Child = stage
        WpfGrid.SetColumn(frame, 0)
        body.Children.Add(frame)

        side = self.build_side_panel()
        WpfGrid.SetColumn(side, 1)
        body.Children.Add(side)
        page.Children.Add(body)
        return page

    def build_side_panel(self):
        panel = DockPanel()
        panel.Margin = Thickness(12, 0, 0, 0)

        legend_card = Border()
        legend_card.Background = Brushes.White
        legend_card.CornerRadius = CornerRadius(8)
        legend_card.Padding = Thickness(12, 8, 12, 8)
        legend_card.Margin = Thickness(0, 0, 0, 10)
        legend = StackPanel()
        legend.Children.Add(text_block(u"SHOW", 10, True, "#64748B", Thickness(0, 0, 0, 4)))
        self.legend_counts = {}
        for key, label, _ in PREVIEW_STATUSES:
            row = DockPanel()
            row.Margin = Thickness(0, 2, 0, 2)
            count = text_block(u"0", 12, True, "#0F172A")
            count.VerticalAlignment = VerticalAlignment.Center
            DockPanel.SetDock(count, Dock.Right)
            row.Children.Add(count)
            self.legend_counts[key] = count
            box = CheckBox()
            box.IsChecked = True
            box.Tag = key
            box.VerticalContentAlignment = VerticalAlignment.Center
            content = StackPanel()
            content.Orientation = Orientation.Horizontal
            swatch = Border()
            swatch.Width = 12
            swatch.Height = 12
            swatch.CornerRadius = CornerRadius(2 if key == "orphan" else 6)
            swatch.Background = SolidColorBrush(status_color(key))
            swatch.Margin = Thickness(2, 0, 8, 0)
            content.Children.Add(swatch)
            content.Children.Add(text_block(label, 12, False, "#334155"))
            box.Content = content
            box.Checked += self.on_kind_toggle
            box.Unchecked += self.on_kind_toggle
            row.Children.Add(box)
            legend.Children.Add(row)
        legend_card.Child = legend
        DockPanel.SetDock(legend_card, Dock.Top)
        panel.Children.Add(legend_card)

        details_card = Border()
        details_card.Background = Brushes.White
        details_card.CornerRadius = CornerRadius(8)
        details_card.Padding = Thickness(12, 10, 12, 10)
        details_card.Margin = Thickness(0, 10, 0, 0)
        details_card.MinHeight = 190
        self.details = StackPanel()
        details_card.Child = self.details
        DockPanel.SetDock(details_card, Dock.Bottom)
        panel.Children.Add(details_card)

        list_card = Border()
        list_card.Background = Brushes.White
        list_card.CornerRadius = CornerRadius(8)
        list_card.Padding = Thickness(12, 8, 12, 8)
        list_panel = DockPanel()
        top = StackPanel()
        self.list_title = text_block(u"", 10, True, "#64748B", Thickness(0, 0, 0, 4))
        top.Children.Add(self.list_title)
        self.search_box = TextBox()
        self.search_box.Height = 26
        self.search_box.Padding = Thickness(4, 3, 4, 3)
        self.search_box.ToolTip = u"Search by type, family or ID"
        self.search_box.TextChanged += self.on_search
        top.Children.Add(self.search_box)
        DockPanel.SetDock(top, Dock.Top)
        list_panel.Children.Add(top)
        self.list_box = ListBox()
        self.list_box.Margin = Thickness(0, 6, 0, 0)
        self.list_box.BorderThickness = Thickness(0)
        self.list_box.SelectionChanged += self.on_list_select
        list_panel.Children.Add(self.list_box)
        list_card.Child = list_panel
        panel.Children.Add(list_card)
        self.show_details(None)
        return panel

    # ---- levels -------------------------------------------------------------
    def fill_levels(self):
        level_ids = set(m.level_id for m in self.markers if m.level_id is not None)
        ordered = sorted([lid for lid in level_ids if lid in self.levels_by_id],
                         key=lambda lid: self.levels_by_id[lid].Elevation)
        self.level_keys = ["ALL"] + ordered
        entry = ComboBoxItem()
        entry.Content = u"All selected levels   ({})".format(len(self.markers))
        self.level_combo.Items.Add(entry)
        for lid in ordered:
            entry = ComboBoxItem()
            entry.Content = u"{}   ({})".format(self.levels_by_id[lid].Name,
                                                sum(1 for m in self.markers if m.level_id == lid))
            self.level_combo.Items.Add(entry)
        default = 0
        try:
            view_level = id_value(active_view.GenLevel.Id) if active_view.GenLevel is not None else None
        except Exception:
            view_level = None
        if view_level in ordered:
            default = self.level_keys.index(view_level)
        elif ordered:
            busiest = max(ordered, key=lambda lid: sum(1 for m in self.markers if m.level_id == lid))
            default = self.level_keys.index(busiest)
        self.default_level_index = default

    def on_loaded(self, sender, args):
        self.level_combo.SelectedIndex = self.default_level_index   # -> on_level_changed

    def on_level_changed(self, sender, args):
        index = self.level_combo.SelectedIndex
        if 0 <= index < len(self.level_keys):
            self.set_level(self.level_keys[index])

    def set_level(self, key):
        self.level_key = key
        self.selected = None
        self.hover = None
        self.show_details(None)
        self.update_context()
        self.refresh(fit=True)

    def level_markers(self):
        if self.level_key == "ALL":
            return list(self.markers)
        return [m for m in self.markers if m.level_id == self.level_key]

    # ---- 3D context: floor slab + walls of the loaded links ------------
    def get_walls(self):
        if self.walls is None:
            self.walls = []
            if self.markers:
                try:
                    self.walls = scan_walls(bounds_of(self.markers, 15.0 * FEET_PER_M))
                except Exception:
                    self.walls = []
        return self.walls

    def get_grids(self):
        if self.grids is None:
            try:
                self.grids = scan_grids()
            except Exception:
                self.grids = []
        return self.grids

    def update_context(self):
        if self.current_context is not None:
            self.viewport.Children.Remove(self.current_context)
            self.current_context = None
        key = (self.level_key, self.show_walls)
        visual = self.context_cache.get(key)
        if visual is None:
            try:
                visual = self.build_context(self.level_key, self.show_walls)
            except Exception:
                visual = None
            if visual is None:
                return
            self.context_cache[key] = visual
        self.viewport.Children.Add(visual)
        self.current_context = visual

    def build_context(self, level_key, with_walls):
        group = Model3DGroup()
        slab = MeshGeometry3D()
        margin = 3.0 * FEET_PER_M
        if level_key == "ALL":
            by_level = {}
            for marker in self.markers:
                if marker.level_id in self.levels_by_id:
                    by_level.setdefault(marker.level_id, []).append(marker)
            for lid, members in by_level.items():
                z = self.levels_by_id[lid].Elevation
                xmin, ymin, xmax, ymax = bounds_of(members, margin)
                add_slab(slab, xmin, ymin, xmax, ymax, z - 0.8, z - 0.15)
        else:
            level = self.levels_by_id.get(level_key)
            members = self.level_markers()
            if level is None or not members:
                return None
            z = level.Elevation
            xmin, ymin, xmax, ymax = bounds_of(members, margin)
            add_slab(slab, xmin, ymin, xmax, ymax, z - 0.8, z - 0.15)
            if with_walls:
                walls = MeshGeometry3D()
                cut = z + WALL_CUT
                count = 0
                for points, half, z0, z1, box in self.get_walls():
                    if z0 > cut or z1 < cut:
                        continue
                    if box[2] < xmin or box[0] > xmax or box[3] < ymin or box[1] > ymax:
                        continue
                    base = max(z0, z)
                    for i in range(len(points) - 1):
                        add_prism(walls, points[i], points[i + 1], half, base, cut)
                    count += 1
                    if count > 20000:
                        break
                if walls.Positions.Count:
                    group.Children.Add(solid_model(walls, Color.FromRgb(148, 163, 184)))
        if slab.Positions.Count:
            group.Children.Add(solid_model(slab, Color.FromRgb(30, 41, 59)))
        visual = ModelVisual3D()
        visual.Content = group
        return visual

    # ---- filtering, list and details -----------------------------------
    def refresh(self, fit=False):
        in_level = self.level_markers()
        counts = {}
        for marker in in_level:
            counts[marker.kind] = counts.get(marker.kind, 0) + 1
        for key, block in self.legend_counts.items():
            block.Text = str(counts.get(key, 0))
        self.shown = sorted([m for m in in_level if m.kind in self.visible_kinds],
                            key=lambda m: DRAW_ORDER[m.kind])
        level = self.levels_by_id.get(self.level_key)
        self.status_text.Text = u"{}   |   {} shown".format(
            level.Name if level is not None else u"All selected levels", len(self.shown))
        self.fill_list()
        if fit:
            self.fit(in_level)
        else:
            self.layer.InvalidateVisual()

    def fill_list(self):
        text = (self.search_box.Text or u"").strip().lower()
        members = [m for m in self.shown if not text or text in m.search]
        members.sort(key=lambda m: (LIST_ORDER[m.kind], m.title.lower(), m.ident))
        self.list_guard = True
        self.list_box.Items.Clear()
        self.list_markers = members[:800]
        for marker in self.list_markers:
            entry = ListBoxItem()
            line = StackPanel()
            line.Orientation = Orientation.Horizontal
            dot = Border()
            dot.Width = 9
            dot.Height = 9
            dot.CornerRadius = CornerRadius(1 if marker.kind == "orphan" else 4.5)
            dot.Background = SolidColorBrush(status_color(marker.kind))
            dot.Margin = Thickness(0, 0, 7, 0)
            dot.VerticalAlignment = VerticalAlignment.Center
            line.Children.Add(dot)
            caption = TextBlock()
            caption.Text = u"{}  {}  ·  {}".format(STATUS_SHORT[marker.kind], marker.title, marker.ident)
            caption.FontSize = 11
            line.Children.Add(caption)
            entry.Content = line
            entry.Padding = Thickness(2, 1, 2, 1)
            self.list_box.Items.Add(entry)
        if len(members) > len(self.list_markers):
            more = ListBoxItem()
            more.Content = u"... {} more - type in the search box".format(len(members) - len(self.list_markers))
            more.IsEnabled = False
            self.list_box.Items.Add(more)
        self.list_guard = False
        self.list_title.Text = u"FIXTURES & SYMBOLS   ({})".format(len(members))

    def on_search(self, sender, args):
        self.fill_list()

    def on_list_select(self, sender, args):
        if self.list_guard:
            return
        index = self.list_box.SelectedIndex
        if 0 <= index < len(self.list_markers):
            self.select(self.list_markers[index], zoom=True, from_list=True)

    def on_kind_toggle(self, sender, args):
        key = sender.Tag
        if sender.IsChecked == True:
            self.visible_kinds.add(key)
        else:
            self.visible_kinds.discard(key)
        self.refresh()

    def on_toggle(self, sender, args):
        tag = sender.Tag
        state = sender.IsChecked == True
        if tag == "walls":
            self.show_walls = state
            self.update_context()
        elif tag == "grids":
            self.show_grids = state
        elif tag == "labels":
            self.show_labels = state
        elif tag == "zones":
            self.show_zones = state
        self.layer.InvalidateVisual()

    def select(self, marker, zoom=False, from_list=False):
        self.selected = marker
        self.show_details(marker)
        if marker is not None and zoom:
            self.target = (marker.x, marker.y, marker.z)
            self.width = min(self.width, 10.0 * FEET_PER_M)
        if marker is not None and not from_list:
            for index, candidate in enumerate(self.list_markers):
                if candidate is marker:
                    self.list_guard = True
                    self.list_box.SelectedIndex = index
                    self.list_box.ScrollIntoView(self.list_box.Items[index])
                    self.list_guard = False
                    break
        self.apply_camera()

    def detail_lines(self, marker):
        if marker.item is None:
            orphan = marker.orphan
            tilted = getattr(orphan, "tilted", False)
            return [
                (u"Symbol", orphan.label),
                (u"Element ID", str(marker.ident)),
                (u"Level", orphan.level_name),
                (u"Circuit", u"connected" if orphan.circuits else u"-"),
                (u"Result", u"Lies on a sloped / vertical face - plan views cannot show it. A flat symbol "
                            u"is placed instead; delete this one on the 'No linked fixture' tab."
                 if tilted else
                 u"No linked fixture within 350 mm. Deleted only if you tick it on the "
                 u"'No linked fixture' tab."),
            ]
        item = marker.item
        lines = [
            (u"Family", item.key[0]),
            (u"Linked ID", str(marker.ident)),
            (u"Level", u"{}  ->  {}".format(item.level.Name if item.level is not None else u"-",
                                            item.target_level.Name if item.target_level is not None else u"-")),
        ]
        above = height_above_level(item)
        if above is not None:
            text = u"{} mm above level".format(int(round(above)))
            if above < MIN_SYMBOL_LIFT * 304.8:
                text += u"  (symbol goes 10 mm above the level)"
            lines.append((u"Height", text))
        kind = marker.kind
        if kind in ("place_type", "place_new"):
            lines.append((u"Result", u"A new 2D symbol is placed on this fixture."))
            lines.append((u"2D type", planned_type_name(item) +
                          (u"   (new type, created in '{}')".format(family.Name) if kind == "place_new" else u"")))
        elif kind == "align":
            lines.append((u"Result", u"The existing symbol moves {} mm onto the fixture - plan only; "
                                     u"height, type and circuit stay.".format(
                                         int(round(getattr(item, "existing_distance", 0.0) * 304.8)))))
            lines.append((u"Symbol", u"{}   (ID {})".format(marker.label, id_value(item.existing_id))))
            lines.append((u"Matched by", getattr(item, "existing_how", u"-")))
        elif kind == "ok":
            lines.append((u"Result", u"A symbol already sits on this fixture - nothing changes."))
            lines.append((u"Symbol", u"{}   (ID {})".format(marker.label, id_value(item.existing_id))))
        else:
            lines.append((u"Result", u"Not placed: " + SKIP_TEXT.get(item.status, item.status)))
        if item.watt_text:
            lines.append((u"Wattage", item.watt_text))
        if id_value(item.fixture.Id) in off_geometry_ids:
            lines.append((u"Position", u"fixture centre (its insertion point is away from its geometry)"))
        return lines

    def show_details(self, marker):
        self.details.Children.Clear()
        self.details.Children.Add(text_block(u"DETAILS", 10, True, "#64748B", Thickness(0, 0, 0, 6)))
        if marker is None:
            self.details.Children.Add(text_block(
                u"Click a marker in the view, or an entry in the list, to see exactly what will happen "
                u"to it.", 12, False, "#64748B"))
            return
        self.details.Children.Add(text_block(marker.title, 14, True, "#0F172A", Thickness(0, 0, 0, 4)))
        chip = Border()
        chip.CornerRadius = CornerRadius(10)
        chip.Background = SolidColorBrush(status_color(marker.kind, 40))
        chip.BorderBrush = SolidColorBrush(status_color(marker.kind))
        chip.BorderThickness = Thickness(1)
        chip.Padding = Thickness(8, 2, 8, 2)
        chip.HorizontalAlignment = HorizontalAlignment.Left
        chip.Margin = Thickness(0, 0, 0, 8)
        chip.Child = text_block(STATUS_LABEL[marker.kind], 11, True, "#0F172A")
        self.details.Children.Add(chip)
        grid = WpfGrid()
        key_column = ColumnDefinition()
        key_column.Width = GridLength(82)
        value_column = ColumnDefinition()
        value_column.Width = GridLength(1, GridUnitType.Star)
        grid.ColumnDefinitions.Add(key_column)
        grid.ColumnDefinitions.Add(value_column)
        for row_index, (key, value) in enumerate(self.detail_lines(marker)):
            definition = RowDefinition()
            definition.Height = GridLength.Auto
            grid.RowDefinitions.Add(definition)
            name = text_block(key, 11, False, "#64748B", Thickness(0, 2, 6, 2))
            text = text_block(value or u"-", 11, False, "#0F172A", Thickness(0, 2, 0, 2))
            WpfGrid.SetRow(name, row_index)
            WpfGrid.SetRow(text, row_index)
            WpfGrid.SetColumn(text, 1)
            grid.Children.Add(name)
            grid.Children.Add(text)
        self.details.Children.Add(grid)

    # ---- camera (orthographic: exact plan look, true 3D when orbiting) --
    def vectors(self):
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        d = (cp * cy, cp * sy, sp)                 # from target towards the camera
        up = (-sp * cy, -sp * sy, cp)
        look = (-d[0], -d[1], -d[2])
        right = (look[1] * up[2] - look[2] * up[1],
                 look[2] * up[0] - look[0] * up[2],
                 look[0] * up[1] - look[1] * up[0])
        return d, up, right

    def apply_camera(self):
        d, up, right = self.vectors()
        distance = self.extent * 3.0 + 300.0
        t = self.target
        self.camera.Position = Point3D(t[0] + d[0] * distance, t[1] + d[1] * distance, t[2] + d[2] * distance)
        self.camera.LookDirection = Vector3D(-d[0], -d[1], -d[2])
        self.camera.UpDirection = Vector3D(up[0], up[1], up[2])
        self.camera.Width = self.width
        self.camera.NearPlaneDistance = 1.0
        self.camera.FarPlaneDistance = distance * 2.0 + self.extent * 2.0
        self.layer.InvalidateVisual()

    def fit(self, members=None):
        members = members if members is not None else self.level_markers()
        if not members:
            self.apply_camera()
            return
        w = max(self.stage.ActualWidth, 400.0)
        h = max(self.stage.ActualHeight, 300.0)
        xs = [m.x for m in members]
        ys = [m.y for m in members]
        zs = [m.z for m in members]
        cx, cy, cz = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2
        self.target = (cx, cy, cz)
        d, up, right = self.vectors()
        along_r = [(m.x - cx) * right[0] + (m.y - cy) * right[1] + (m.z - cz) * right[2] for m in members]
        along_u = [(m.x - cx) * up[0] + (m.y - cy) * up[1] + (m.z - cz) * up[2] for m in members]
        span_r = max(along_r) - min(along_r)
        span_u = max(along_u) - min(along_u)
        self.width = max(span_r, span_u * w / h) * 1.12 + 3.0 * FEET_PER_M
        self.apply_camera()

    def on_plan(self, sender, args):
        self.yaw = self.view_yaw
        self.pitch = math.pi / 2
        self.fit()

    def on_iso(self, sender, args):
        self.yaw = self.view_yaw - math.pi / 4
        self.pitch = math.radians(35)
        self.fit()

    def on_fit(self, sender, args):
        self.fit()

    def on_size_changed(self, sender, args):
        if self.fit_pending and self.stage.ActualWidth > 50:
            self.fit_pending = False
            self.fit()
        else:
            self.apply_camera()

    # ---- mouse ------------------------------------------------------------
    def pick(self, x, y, radius=10.0):
        best, best_distance = None, radius * radius
        for sx, sy, marker in self.screen:
            distance = (sx - x) ** 2 + (sy - y) ** 2
            if distance <= best_distance:
                best, best_distance = marker, distance
        return best

    def on_mouse_down(self, sender, args):
        position = args.GetPosition(self.stage)
        if args.ChangedButton == MouseButton.Left and args.ClickCount == 2:
            marker = self.pick(position.X, position.Y)
            if marker is not None:
                self.select(marker, zoom=True)
            return
        mode = "orbit" if args.ChangedButton == MouseButton.Right else "pan"
        self.drag = {"mode": mode, "x": position.X, "y": position.Y, "moved": False,
                     "left": args.ChangedButton == MouseButton.Left}
        self.stage.CaptureMouse()

    def on_mouse_move(self, sender, args):
        position = args.GetPosition(self.stage)
        drag = self.drag
        if drag is not None:
            dx, dy = position.X - drag["x"], position.Y - drag["y"]
            if not drag["moved"] and abs(dx) + abs(dy) < 4:
                return
            drag["moved"] = True
            drag["x"], drag["y"] = position.X, position.Y
            if drag["mode"] == "pan":
                scale = max(self.stage.ActualWidth, 1.0) / self.width
                d, up, right = self.vectors()
                t = self.target
                self.target = (t[0] - right[0] * dx / scale + up[0] * dy / scale,
                               t[1] - right[1] * dx / scale + up[1] * dy / scale,
                               t[2] - right[2] * dx / scale + up[2] * dy / scale)
            else:
                self.yaw -= dx * 0.01
                self.pitch = max(math.radians(10), min(math.pi / 2, self.pitch + dy * 0.01))
            self.apply_camera()
            return
        marker = self.pick(position.X, position.Y)
        if marker is not self.hover:
            self.hover = marker
            self.layer.InvalidateVisual()
        self.stage.Cursor = Cursors.Hand if marker is not None else Cursors.Arrow

    def on_mouse_up(self, sender, args):
        drag = self.drag
        if drag is None:
            return
        self.drag = None
        self.stage.ReleaseMouseCapture()
        if not drag["moved"] and drag["left"]:
            position = args.GetPosition(self.stage)
            self.select(self.pick(position.X, position.Y))

    def on_mouse_leave(self, sender, args):
        if self.hover is not None and self.drag is None:
            self.hover = None
            self.layer.InvalidateVisual()

    def on_wheel(self, sender, args):
        position = args.GetPosition(self.stage)
        w, h = max(self.stage.ActualWidth, 1.0), max(self.stage.ActualHeight, 1.0)
        new_width = max(1.0 * FEET_PER_M, min(5000.0 * FEET_PER_M,
                                                self.width * (0.8 if args.Delta > 0 else 1.25)))
        factor = new_width / self.width
        scale = w / self.width
        d, up, right = self.vectors()
        ox, oy = (position.X - w / 2) / scale, (h / 2 - position.Y) / scale
        t = self.target
        keep = 1.0 - factor                         # keep the point under the cursor in place
        self.target = (t[0] + (right[0] * ox + up[0] * oy) * keep,
                       t[1] + (right[1] * ox + up[1] * oy) * keep,
                       t[2] + (right[2] * ox + up[2] * oy) * keep)
        self.width = new_width
        self.apply_camera()
        args.Handled = True

    # ---- overlay drawing ---------------------------------------------------
    def render_overlay(self, dc, w, h):
        self.screen = []
        if w < 10 or h < 10:
            return
        try:
            self.draw_overlay(dc, w, h)
        except Exception as error:
            if self.render_error is None:
                self.render_error = u"{}".format(error)
                try:
                    self.status_text.Text = u"Preview drawing problem: " + self.render_error
                except Exception:
                    pass

    def draw_overlay(self, dc, w, h):
        d, up, right = self.vectors()
        tx, ty, tz = self.target
        scale = w / self.width
        cx, cy = w / 2.0, h / 2.0
        r0, r1, r2 = right
        u0, u1, u2 = up

        def project(x, y, z):
            rx, ry, rz = x - tx, y - ty, z - tz
            return (cx + (rx * r0 + ry * r1 + rz * r2) * scale,
                    cy - (rx * u0 + ry * u1 + rz * u2) * scale)

        level = self.levels_by_id.get(self.level_key)

        # grids with bubbles (on the level plane)
        if self.show_grids and level is not None:
            z = level.Elevation
            for name, a, b in self.get_grids():
                pa = project(a[0], a[1], z)
                pb = project(b[0], b[1], z)
                dc.DrawLine(GRID_PEN, WinPoint(pa[0], pa[1]), WinPoint(pb[0], pb[1]))
                vx, vy = pa[0] - pb[0], pa[1] - pb[1]
                length = math.sqrt(vx * vx + vy * vy)
                if length < 1.0:
                    continue
                vx, vy = vx / length, vy / length
                label = formatted(name, 10.0, "grid", True)
                for (px, py), sign in ((pa, 1.0), (pb, -1.0)):
                    bx, by = px + vx * sign * 13.0, py + vy * sign * 13.0
                    if -20 < bx < w + 20 and -20 < by < h + 20:
                        dc.DrawEllipse(BUBBLE_FILL, GRID_BUBBLE_PEN, WinPoint(bx, by), 11.0, 11.0)
                        dc.DrawText(label, WinPoint(bx - label.Width / 2, by - label.Height / 2))

        # 350 mm matching zones
        zone = EXISTING_RADIUS * scale
        if self.show_zones and zone >= 4.0:
            for marker in self.shown:
                if marker.kind == "orphan":
                    continue
                x, y = project(marker.x, marker.y, marker.z)
                if -zone < x < w + zone and -zone < y < h + zone:
                    dc.DrawEllipse(None, ZONE_PEN, WinPoint(x, y), zone, zone)

        # markers (draw order: OK at the bottom, problems on top)
        screen = []
        for marker in self.shown:
            x, y = project(marker.x, marker.y, marker.z)
            kind = marker.kind
            if kind == "align" and marker.sym is not None:
                sx, sy = project(marker.sym[0], marker.sym[1], marker.sym[2])
                if max(x, sx) < -40 or min(x, sx) > w + 40 or max(y, sy) < -40 or min(y, sy) > h + 40:
                    continue
                dc.DrawLine(ALIGN_PEN, WinPoint(sx, sy), WinPoint(x, y))
                vx, vy = x - sx, y - sy
                length = math.sqrt(vx * vx + vy * vy)
                if length > 12.0:                       # arrow head at the fixture
                    vx, vy = vx / length, vy / length
                    hx, hy = x - vx * 7.0, y - vy * 7.0
                    for side in (1.0, -1.0):
                        dc.DrawLine(ALIGN_PEN, WinPoint(hx, hy),
                                    WinPoint(hx - vx * 7.0 - vy * 5.0 * side, hy - vy * 7.0 + vx * 5.0 * side))
                dc.DrawRectangle(FILL["align"], OUTLINE_PEN, Rect(sx - 4.0, sy - 4.0, 8.0, 8.0))
                dc.DrawEllipse(None, ALIGN_RING, WinPoint(x, y), 6.0, 6.0)
            else:
                if x < -40 or x > w + 40 or y < -40 or y > h + 40:
                    continue
                if kind == "orphan":
                    dc.DrawRectangle(FILL["orphan"], OUTLINE_PEN, Rect(x - 4.5, y - 4.5, 9.0, 9.0))
                else:
                    radius = 4.0 if kind == "ok" else 5.5
                    dc.DrawEllipse(FILL[kind], OUTLINE_PEN, WinPoint(x, y), radius, radius)
            screen.append((x, y, marker))
        self.screen = screen

        # type labels when zoomed in enough to read them
        if self.show_labels and scale * FEET_PER_M >= 16.0:
            drawn = 0
            for x, y, marker in screen:
                if 0 <= x <= w and 0 <= y <= h and marker.label:
                    label = formatted(marker.label, 10.0, "label")
                    dc.DrawText(label, WinPoint(x + 8.0, y - label.Height - 1.0))
                    drawn += 1
                    if drawn >= 600:
                        break

        # selection
        if self.selected is not None:
            x, y = project(self.selected.x, self.selected.y, self.selected.z)
            dc.DrawEllipse(None, SELECT_HALO, WinPoint(x, y), 12.0, 12.0)
            dc.DrawEllipse(None, SELECT_PEN, WinPoint(x, y), 12.0, 12.0)

        # hover card
        if self.hover is not None:
            marker = self.hover
            x, y = project(marker.x, marker.y, marker.z)
            lines = [formatted(marker.title, 12.0, "text", True),
                     formatted(STATUS_LABEL[marker.kind], 11.0, marker.kind, True)]
            if marker.label and marker.label != marker.title:
                lines.append(formatted(u"2D type: {}".format(marker.label), 11.0, "label"))
            lines.append(formatted(u"ID {}".format(marker.ident), 10.0, "muted"))
            card_w = max(line.Width for line in lines) + 20.0
            card_h = sum(line.Height for line in lines) + 14.0
            bx = x + 16.0 if x + 16.0 + card_w < w else x - 16.0 - card_w
            by = y + 16.0 if y + 16.0 + card_h < h else y - 16.0 - card_h
            dc.DrawRoundedRectangle(CARD_FILL, CARD_PEN, Rect(bx, by, card_w, card_h), 6.0, 6.0)
            ty_ = by + 7.0
            for line in lines:
                dc.DrawText(line, WinPoint(bx + 10.0, ty_))
                ty_ += line.Height

        # scale bar
        per_metre = scale * FEET_PER_M
        length_m = 1000.0
        for candidate in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
            if candidate * per_metre >= 70.0:
                length_m = candidate
                break
        bar = length_m * per_metre
        x0, y0 = 16.0, h - 20.0
        dc.DrawLine(SCALE_PEN, WinPoint(x0, y0), WinPoint(x0 + bar, y0))
        dc.DrawLine(SCALE_PEN, WinPoint(x0, y0 - 5.0), WinPoint(x0, y0 + 1.0))
        dc.DrawLine(SCALE_PEN, WinPoint(x0 + bar, y0 - 5.0), WinPoint(x0 + bar, y0 + 1.0))
        caption = formatted(u"{:g} m".format(length_m), 11.0, "text", True)
        dc.DrawText(caption, WinPoint(x0 + bar + 8.0, y0 - caption.Height / 2.0 - 2.0))

    # ---- type matching table ---------------------------------------------
    def build_matching(self):
        grid = WpfGrid()
        grid.Margin = Thickness(10)
        widths = (2.2, 0.4, 1.8, 0.8, 1.0, 0.55, 0.65)   # last column: existing
        for width in widths:
            column = ColumnDefinition()
            column.Width = GridLength(width, GridUnitType.Star)
            grid.ColumnDefinitions.Add(column)
        headers = ("Linked fixture (Family : Type)", "Qty", "Placed as 2D type",
                   "Match", "Wattage (link)", "New", "Align / ok")
        all_rows = [headers] + [(r["linked"], str(r["qty"]), r["target"],
                                 r["match"], r["watts"], str(r["new"]),
                                 str(r["existing"]))
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
            u"{} existing symbol(s) within 350 mm will be ALIGNED to the linked fixture "
            u"(moved only - type, parameters and circuits stay the same).".format(len(to_align)),
            u"{} existing symbol(s) are already exactly on the linked fixture.".format(len(already)),
            u"{} use your existing types, {} new type(s) will be created.".format(
                sum(1 for k in selected_group_keys if mapping[k][0] == "existing"),
                sum(1 for k in selected_group_keys if mapping[k][0] == "new")),
        ]
        lines.append(u"Aligning only moves the symbol: circuit membership is checked before and after each "
                     u"move and the move is undone if it would change a circuit. Wires stay connected.")
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

    def deletion_list(self):
        if self.delete_box.IsChecked != True:
            return []
        return orphans_to_delete(self.other_box.IsChecked == True, self.circuit_box.IsChecked == True)

    def refresh_button(self):
        align = bool(to_align) and self.align_box.IsChecked == True
        to_delete = self.deletion_list()
        candidates = orphans_to_delete(self.other_box.IsChecked == True, self.circuit_box.IsChecked == True)
        self.orphan_count.Text = u"{} symbol(s) {} deleted with these settings.".format(
            len(candidates), u"WILL BE" if self.delete_box.IsChecked == True else u"would be")
        parts = []
        if to_place:
            parts.append(u"Place {} new".format(len(to_place)))
        if align:
            parts.append(u"Align {} existing".format(len(to_align)))
        if to_delete:
            parts.append(u"Delete {}".format(len(to_delete)))
        self.place_button.Content = u", ".join(parts) if parts else u"Nothing to do"
        self.place_button.IsEnabled = bool(parts)

    def on_place(self, sender, args):
        self.accepted = True
        self.copy_watts = self.watt_box.IsChecked == True
        self.align_existing = self.align_box.IsChecked == True
        self.delete_ids = [o.id for o in self.deletion_list()]
        self.window.Close()

    def build_orphans(self):
        page = StackPanel()
        page.Margin = Thickness(12)
        page.Children.Add(text_block(
            u"Existing symbols on the selected level(s) with NO linked lighting fixture within 350 mm "
            u"(checked against every linked fixture, any level). Pink squares in the Preview.",
            12, False, "#334155", Thickness(0, 0, 0, 6)))
        page.Children.Add(self.delete_box)
        page.Children.Add(self.other_box)
        page.Children.Add(self.circuit_box)
        page.Children.Add(self.orphan_count)

        grid = WpfGrid()
        for width in (2.6, 0.9, 1.2, 1.2, 1.0):
            column = ColumnDefinition()
            column.Width = GridLength(width, GridUnitType.Star)
            grid.ColumnDefinitions.Add(column)
        rows = [("Family : Type", "Element ID", "Level", "Source", "Circuit")]
        for o in orphans[:1000]:
            rows.append((o.label, str(id_value(o.id)), o.level_name,
                         family.Name if o.in_family else u"other family (placed by this tool)",
                         u"connected" if o.circuits else u"-"))
        for row_index, values in enumerate(rows):
            definition = RowDefinition()
            definition.Height = GridLength.Auto
            grid.RowDefinitions.Add(definition)
            for column_index, value in enumerate(values):
                block = text_block(value, 12, row_index == 0, "#0F172A", Thickness(0, 2, 10, 2))
                WpfGrid.SetRow(block, row_index)
                WpfGrid.SetColumn(block, column_index)
                grid.Children.Add(block)
        scroll = ScrollViewer()
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        scroll.MaxHeight = 360
        scroll.Content = grid
        frame = Border()
        frame.Background = Brushes.White
        frame.CornerRadius = CornerRadius(8)
        frame.Padding = Thickness(8)
        frame.Child = scroll
        page.Children.Add(frame)
        return page

    def show(self):
        self.window.ShowDialog()
        return self.accepted


preview = PreviewWindow()
if not preview.show():
    report_picked()       # still explain the selected linked fixtures
    script.exit()
copy_watts = preview.copy_watts
align_existing = getattr(preview, "align_existing", False)
delete_ids = getattr(preview, "delete_ids", [])


# -----------------------------------------------------------------------------
# 8. Place + copy wattage (one transaction)
# -----------------------------------------------------------------------------
placed = 0
watt_written = 0
watt_missing_target = 0
created_types = []
failed = []
placement_notes = []   # [linked type, linked id, note] - placed, with a remark


def closest_symbol(key):
    """Best-named existing type of the chosen family (any score), else the first."""
    best, best_score = None, -1
    for candidate in family_symbols:
        score = match_score(type_name(candidate), key[0], key[1])
        if score > best_score:
            best, best_score = candidate, score
    return best or base_symbol


placed_per_type = {}
type_watts = {}        # type-level wattage: symbol id -> set of values written


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


aligned = []          # [linked type, linked id, symbol link, moved in plan mm, moved in Z mm]
deleted = []          # [family : type, element id, level]


transaction = Transaction(doc, "Place / align 2D lighting symbols from link")
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
                False
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
            # e.g. the family is borrowed by another user (workshared model):
            # still place a symbol, using the closest existing type
            fallback = closest_symbol(key)
            symbol_for[key] = fallback
            placement_notes.append([u"{} : {}".format(*key), u"-",
                                    u"new type '{}' could not be created ({}) - placed as existing type '{}'"
                                    .format(value, error, type_name(fallback))])

    for symbol in symbol_for.values():
        try:
            if not symbol.IsActive:
                symbol.Activate()
        except Exception:
            pass
    doc.Regenerate()

    # align existing symbols within 350 mm: MOVE only (no type / parameter changes)
    for item in (to_align if align_existing else []):
        source_label = u"{} : {}".format(*item.key)
        instance = item.existing_instance
        if point_distance(instance.Location.Point, item.point, True) > COMMENT_MATCH_RADIUS:
            failed.append([source_label, id_value(item.fixture.Id),
                           u"align skipped: symbol is more than 1.5 m away - left where it was"])
            continue
        sub = SubTransaction(doc)
        sub.Start()
        try:
            circuits_before = connected_circuit_ids(instance)
            start = instance.Location.Point
            target = align_target(item)
            was_pinned = False
            try:
                was_pinned = instance.Pinned
                if was_pinned:
                    instance.Pinned = False      # pinned symbols cannot be moved
            except Exception:
                pass
            if point_distance(start, target, True) > ALIGN_TOLERANCE:
                ElementTransformUtils.MoveElement(doc, instance.Id, target - start)
                doc.Regenerate()
            if was_pinned:
                try:
                    instance.Pinned = True
                except Exception:
                    pass
            end = instance.Location.Point
            if point_distance(end, target, True) > ALIGN_TOLERANCE:
                raise Exception("Revit did not move it onto the linked fixture (hosted / constrained?)")
            if connected_circuit_ids(instance) != circuits_before:
                raise Exception("the move would change its circuit - left where it was")
            sub.Commit()
            aligned.append([source_label, id_value(item.fixture.Id), output.linkify(instance.Id),
                            int(round(point_distance(start, end, True) * 304.8)),
                            int(round((end.Z - start.Z) * 304.8))])
        except Exception as error:
            sub.RollBack()
            failed.append([source_label, id_value(item.fixture.Id), u"align: {}".format(error)])

    # delete existing symbols that have no linked fixture (only if ticked in the preview)
    orphan_by_id = dict((id_value(o.id), o) for o in orphans)
    for element_id in delete_ids:
        orphan = orphan_by_id.get(id_value(element_id))
        sub = SubTransaction(doc)
        sub.Start()
        try:
            doc.Delete(element_id)
            sub.Commit()
            deleted.append([orphan.label if orphan else u"-", id_value(element_id),
                            orphan.level_name if orphan else u"-"])
        except Exception as error:
            sub.RollBack()
            failed.append([orphan.label if orphan else u"-", id_value(element_id), u"delete: {}".format(error)])

    for item in to_place:
        symbol = symbol_for.get(item.key)
        source_label = u"{} : {}".format(*item.key)
        if symbol is None:
            symbol = closest_symbol(item.key)
            symbol_for[item.key] = symbol
            try:
                if not symbol.IsActive:
                    symbol.Activate()
                    doc.Regenerate()
            except Exception:
                pass
        fid = id_value(item.fixture.Id)
        sub = SubTransaction(doc)
        sub.Start()
        try:
            if view_based:
                instance = place_in_view(
                    symbol, item.point, item.target_view, item.target_level
                )
            else:
                instance = place_in_model(
                    link, transform, item.fixture, symbol, model_point(item),
                    levels, item.target_level
                )
        except Exception as error:
            sub.RollBack()
            failed.append([source_label, fid, u"Revit refused to place it: {}".format(error)])
            continue

        # Optional steps: a problem here is reported but NEVER removes the symbol.
        def rotate_step():
            location = instance.Location
            if rotate and abs(item.rotation) > 1e-6 and isinstance(location, LocationPoint) \
                    and not is_face_hosted(instance):
                origin = location.Point
                ElementTransformUtils.RotateElement(
                    doc, instance.Id, Line.CreateBound(origin, origin + XYZ.BasisZ), item.rotation)

        for step_name, step in (
                (u"rotation", rotate_step),
                (u"elevation", lambda: check_elevation(instance, item)),
                (u"comments", lambda: write_source_comments(instance, item)),
                (u"wattage", lambda: apply_watts(instance, item))):
            try:
                step()
            except Exception as error:
                placement_notes.append([source_label, fid,
                                        u"placed, but {} not set: {}".format(step_name, error)])
        try:
            sub.Commit()
        except Exception as error:
            try:
                sub.RollBack()
            except Exception:
                pass
            failed.append([source_label, fid, u"Revit refused to place it: {}".format(error)])
            continue
        item.placed_id = instance.Id
        placed += 1
        label = type_name(symbol)
        placed_per_type[label] = placed_per_type.get(label, 0) + 1

    if placed == 0 and not aligned and not deleted and not created_types:
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
                       columns=["2D type", "Placed"], title="New symbols placed per type")
if deleted:
    output.print_table(table_data=deleted, columns=["Family : Type", "Deleted element ID", "Level"],
                       title=u"Deleted: existing symbols with no linked fixture ({})".format(len(deleted)))
if aligned:
    output.print_table(
        table_data=aligned[:500],
        columns=["Linked type", "Linked ID", "Existing symbol (click)", "Moved in plan (mm)", "Moved in height (mm)"],
        title=u"Existing symbols aligned to the linked fixture ({})".format(len(aligned)))
if created_types:
    output.print_table(table_data=[[n] for n in created_types], columns=["New type"], title="New types created")
if conflicts:
    output.print_table(table_data=conflicts, columns=["2D type", "Different wattages written"],
                       title="Wattage is a TYPE parameter but the linked fixtures differ - make it an instance parameter")
if failed:
    output.print_table(table_data=failed, columns=["Linked type", "Linked ID", "Reason"], title="Not placed")
if placement_notes:
    output.print_table(table_data=placement_notes[:1000], columns=["Linked type", "Linked ID", "Note"],
                       title=u"Placed, with a note ({})".format(len(placement_notes)))
report_picked()

skipped_rows = []
for item in items:
    if item.status in ("place", "outside", "existing"):
        continue          # "existing" = a symbol already sits on the fixture
    skipped_rows.append([u"{} : {}".format(*item.key), id_value(item.fixture.Id),
                         item.target_level.Name if item.target_level is not None else u"-",
                         why(item),
                         output.linkify(item.existing_id) if item.existing_id is not None else u"-"])
for item in to_place:
    reason = why(item)
    if reason != u"placed":
        skipped_rows.append([u"{} : {}".format(*item.key), id_value(item.fixture.Id),
                             item.target_level.Name, reason, u"-"])
if skipped_rows:
    output.print_table(
        table_data=skipped_rows[:1000],
        columns=["Linked type", "Linked ID", "Host level", "Result", "Existing symbol (click)"],
        title=u"Linked fixtures that did NOT get a new symbol ({})".format(len(skipped_rows)))

if off_geometry_ids:
    rows_off = [[u"{} : {}".format(*i.key), id_value(i.fixture.Id), why(i)]
                for i in items if id_value(i.fixture.Id) in off_geometry_ids]
    output.print_table(
        table_data=rows_off[:1000], columns=["Linked type", "Linked ID", "Result"],
        title=u"Insertion point away from the fixture geometry ({}) - symbol placed at the "
              u"fixture's centre instead".format(len(rows_off)))

zero_rows = []
for item in items:
    above = height_above_level(item)
    if above is not None and above < MIN_SYMBOL_LIFT * 304.8:
        zero_rows.append([u"{} : {}".format(*item.key), id_value(item.fixture.Id),
                          item.target_level.Name, int(round(above)),
                          item.status if item.status not in ("existing", "align")
                          else u"{} (existing symbol {})".format(item.status, id_value(item.existing_id))])
if zero_rows:
    output.print_table(
        table_data=zero_rows[:500],
        columns=["Linked type", "Linked ID", "Host level", "Height above level (mm)", "Result"],
        title=u"Zero-elevation fixtures ({}) - model symbols kept {} mm above the level so the "
              u"plan view shows them".format(len(zero_rows), int(MIN_SYMBOL_LIFT * 304.8)))
if elevation_problems:
    output.print_table(
        table_data=elevation_problems,
        columns=["Linked type", "Linked ID", "Linked elevation (mm)", "Host symbol elevation (mm)"],
        title="Elevation could not be matched (hosted family? use a non-hosted 2D family)")

summary = [
    u"{} symbol(s) placed.".format(placed),
    u"{} new type(s) created.".format(len(created_types)),
    u"{} existing symbol(s) within 350 mm aligned to the linked fixture{}.".format(
        len(aligned), u"" if align_existing or not to_align else u" (aligning was switched off)"),
    u"{} existing symbol(s) were already on the linked fixture.".format(len(already)),
    u"{} existing symbol(s) with no linked fixture deleted ({} found).".format(len(deleted), len(orphans)),
    u"Aligned symbols were only moved: type, parameters and circuits unchanged.",
]
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
    elif placed:
        summary.append(u"Elevation checked: every NEW symbol is at the linked fixture's "
                       u"elevation (within 1 mm).")
    if zero_rows:
        summary.append(u"{} zero-elevation fixture(s): symbols placed {} mm above the level "
                       u"so they are visible in plan.".format(len(zero_rows), int(MIN_SYMBOL_LIFT * 304.8)))
if failed:
    summary.append(u"{} problem(s) - see the output window.".format(len(failed)))
if placement_notes:
    summary.append(u"{} note(s): symbols placed but e.g. a new type, rotation or wattage could not be "
                   u"set - see 'Placed, with a note' in the output window.".format(len(placement_notes)))
if tilted_records:
    summary.append(u"{} existing symbol(s) lie on a sloped / vertical face, so plan views cannot show "
                   u"them - they were ignored and flat symbols placed. They are listed on the "
                   u"'No linked fixture' tab (marked 'tilted') to delete.".format(len(tilted_records)))
if off_geometry_ids:
    summary.append(u"{} fixture(s) have their insertion point away from their geometry - "
                   u"symbols go to the fixture centre (older symbols beside them are aligned "
                   u"when 'Align existing' is ticked).".format(len(off_geometry_ids)))
if picked_ids or picked_other_link:
    summary.append(u"Your selected linked fixture(s): see 'why' table in the output window.")
    for item in items:
        if id_value(item.fixture.Id) in picked_ids:
            summary.append(u"  ID {}: {}".format(id_value(item.fixture.Id), why(item)))
    for text in picked_other_link:
        summary.append(u"  {}: in ANOTHER link - Shift+Click the button to pick that link.".format(text))
# -----------------------------------------------------------------------------
# Every linked fixture on the selected levels that still has NO symbol on it,
# grouped by reason, so the popup alone explains what was skipped.
# -----------------------------------------------------------------------------
def reason_group(item):
    if item.status in STATUS_TEXT:
        return STATUS_TEXT[item.status]
    if item.status == "align":
        moved = any(r[1] == id_value(item.fixture.Id) for r in aligned)
        if moved:
            return None
        how = getattr(item, "existing_how", u"")
        if "Comments" in how:
            return u"a symbol placed for it earlier is elsewhere and was NOT moved"
        return u"another symbol within 350 mm was counted for it and was NOT moved"
    if item.status == "place":
        for row in failed:
            if row[1] == id_value(item.fixture.Id):
                return u"placing failed: " + row[2][:250]
    return None


still_hidden = []
try:
    if visible_in_view is not None and not view_based:
        visible_after = set(id_value(e.Id) for e in
                            FilteredElementCollector(doc, active_view.Id).OfClass(FamilyInstance))
        for item in items:
            if item.target_level is None or item.target_level.Id != view_level_id:
                continue
            symbol_id = getattr(item, "placed_id", None) or item.existing_id
            if symbol_id is not None and doc.GetElement(symbol_id) is not None \
                    and id_value(symbol_id) not in visible_after:
                still_hidden.append(item)
except Exception:
    still_hidden = []
STILL_HIDDEN = u"its symbol exists but this plan view does NOT show it (view range / filter / workset)"


groups_why = {}
for item in still_hidden:
    groups_why.setdefault(STILL_HIDDEN, []).append(item)
for item in items:
    if item.status == "outside":
        continue
    reason = reason_group(item)
    if reason and item not in still_hidden:
        groups_why.setdefault(reason, []).append(item)
if groups_why:
    summary.append(u"")
    summary.append(u"STILL WITHOUT A SYMBOL ON THE FIXTURE:")
    for reason, group in sorted(groups_why.items(), key=lambda kv: -len(kv[1])):
        type_names = sorted(set(i.key[1] for i in group))
        summary.append(u"- {} x {}  [{}{}]".format(
            len(group), reason, u", ".join(type_names[:4]), u", ..." if len(type_names) > 4 else u""))
    if any(u"NOT moved" in r for r in groups_why):
        summary.append(u"  -> tick 'Align existing' in the preview, or see 'Not placed' in the output "
                       u"window for why the move failed (grouped / constrained symbol).")
    output.print_table(
        table_data=[[u"{} : {}".format(*i.key), id_value(i.fixture.Id), reason,
                     output.linkify(getattr(i, "placed_id", None) or i.existing_id)
                     if (getattr(i, "placed_id", None) or i.existing_id) is not None else u"-"]
                    for reason, group in groups_why.items() for i in group][:1000],
        columns=["Linked type", "Linked ID", "Why no symbol", "Symbol counted for it (click)"],
        title=u"Still without a symbol on the fixture ({})".format(sum(len(g) for g in groups_why.values())))

forms.alert(u"\n".join(summary) + u"\n\nOne Ctrl+Z undoes everything.", title="Light Placement 2D")
