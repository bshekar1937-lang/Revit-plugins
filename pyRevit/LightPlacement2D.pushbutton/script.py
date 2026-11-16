# -*- coding: utf-8 -*-
__title__ = "LIGHT\nPLACEMENT 2D"
__doc__ = (
    "AUTOMATIC: places your 2D lighting symbols at every lighting fixture of the "
    "linked model - no picking. The link, the 2D family and the type for each "
    "linked fixture are found automatically (types you already created are "
    "reused, missing ones are created) and the linked WATTAGE is copied. A 3D "
    "preview is shown before anything is placed. Shift+Click to change the "
    "remembered link / 2D family."
)

# =============================================================================
# LIGHT PLACEMENT 2D  -  fully automatic
#   Click            -> runs with no questions:
#     * link       : the loaded link that contains lighting fixtures
#                    (the one used last time if there are several)
#     * 2D family  : the family whose existing types match the linked fixture
#                    types best (remembered for next time)
#     * types      : each linked "Family : Type" is matched to the closest
#                    existing type name (spaces, dashes, case and word order
#                    ignored); no match -> a new type named after the linked type
#     * position   : centre of each linked fixture, rotated to match it
#     * duplicates : positions that already have the same symbol are skipped,
#                    so running it again only adds what is missing
#     * wattage    : read from each linked fixture (instance or type) and
#                    written to the placed 2D symbol (same parameter name, or
#                    Wattage / Power / Apparent Load ...), units converted
#     * preview    : a window with a 3D preview, the type matching (with
#                    wattage) and a summary - nothing changes until you press
#                    "Place symbols"
#   Shift+Click      -> choose the link / 2D family again (saved for next runs)
#
# The output window lists every linked type and the 2D type it was matched to.
# One transaction: a single Ctrl+Z undoes everything.
# Model families (2D lighting fixture families) are placed at each fixture's
# height; view-specific families (detail items / generic annotations) are
# placed in the ACTIVE PLAN VIEW.
# Revit 2023 - 2027, pyRevit (IronPython engine).
# =============================================================================

import math

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
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


def is_near(a, b, plan_only):
    dz = 0.0 if plan_only else a.Z - b.Z
    return math.sqrt((a.X - b.X) ** 2 + (a.Y - b.Y) ** 2 + dz ** 2) < DUPLICATE_TOLERANCE


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


def set_offset(instance, offset):
    for bip in (BuiltInParameter.INSTANCE_ELEVATION_PARAM,
                BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM):
        parameter = instance.get_Parameter(bip)
        if parameter and not parameter.IsReadOnly and parameter.StorageType == StorageType.Double:
            parameter.Set(offset)
            return


def is_face_hosted(instance):
    try:
        return instance.HostFace is not None
    except Exception:
        return False


def place_in_model(link, transform, fixture, symbol, point, levels):
    errors = []
    level = level_for(point.Z, levels)

    # a) unhosted / level-based / work-plane-based families
    if level and symbol.Family.FamilyPlacementType != FamilyPlacementType.OneLevelBasedHosted:
        try:
            instance = doc.Create.NewFamilyInstance(point, symbol, level, StructuralType.NonStructural)
            set_offset(instance, point.Z - level.Elevation)
            return instance
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


def place_in_view(symbol, point):
    return doc.Create.NewFamilyInstance(XYZ(point.X, point.Y, 0), symbol, active_view)


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
# 2. 2D family (automatic: best name match, remembered)
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
                "Load your 2D family first (Insert > Load Family).", exitscript=True)

linked_family_names = set(norm(k[0]) for k in group_keys)


def family_fit(item):
    family, symbols, _ = item
    if norm(family.Name) in linked_family_names:
        return -1        # that is the linked 3D family itself, not a 2D symbol family
    matched = sum(1 for key in group_keys if best_symbol(symbols, key)[0] is not None)
    bonus = 0.5 if "2d" in family.Name.lower() or "symbol" in family.Name.lower() else 0
    return matched + bonus


chosen_family = None
remembered = getattr(config, "family_name", None)
if remembered and not choose_again:
    chosen_family = next((f for f in families if f[0].Name == remembered), None)
if chosen_family is None and not choose_again:
    ranked = sorted(families, key=family_fit, reverse=True)
    if family_fit(ranked[0]) >= 1:
        chosen_family = ranked[0]
if chosen_family is None:
    ranked = sorted(families, key=family_fit, reverse=True)
    picked = forms.SelectFromList.show(
        [Option(u"{}:  {}".format(f[2], f[0].Name), f) for f in ranked], name_attr="name",
        title="Pick your 2D symbol family ONCE (remembered; Shift+Click to change)",
        button_name="Use this family")
    if not picked:
        script.exit()
    chosen_family = picked.value
family, family_symbols, _ = chosen_family
config.family_name = family.Name
script.save_config()

view_based = family.FamilyPlacementType == FamilyPlacementType.ViewBased
if view_based and not isinstance(active_view, ViewPlan):
    forms.alert("'{}' is a view-specific family (detail item / annotation).\n\n"
                "Open a floor or ceiling plan view first, then run the tool again.".format(family.Name),
                exitscript=True)
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
use_centre = True
rotate = True
skip_dupes = True
write_comments = True
only_view_level = True

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
view_level = active_view.GenLevel if view_based else None

# existing symbols of the chosen family: (symbol id, point, instance id)
existing = []
collector = FilteredElementCollector(doc, active_view.Id) if view_based else FilteredElementCollector(doc)
for instance in collector.OfClass(FamilyInstance):
    symbol = instance.Symbol
    if symbol and symbol.Family and symbol.Family.Id == family.Id \
            and isinstance(instance.Location, LocationPoint):
        existing.append((type_name(symbol), instance.Location.Point, instance.Id))


class FixtureItem(object):
    def __init__(self, key, fixture):
        self.key = key
        self.fixture = fixture
        self.point, self.rotation = placement_of(fixture, transform, use_centre)
        self.watts, self.watt_text, self.watt_name = linked_wattage(fixture)
        self.status = "place"          # place | existing | outside | noloc
        self.existing_id = None


items = []
for key in group_keys:
    kind, value = mapping[key]
    target_name = type_name(value) if kind == "existing" else value
    for fixture in groups[key]:
        item = FixtureItem(key, fixture)
        if item.point is None:
            item.status = "noloc"
        elif view_based and only_view_level and view_level is not None and levels \
                and level_for(item.point.Z, levels).Id != view_level.Id:
            item.status = "outside"
        elif skip_dupes:
            for name, point, instance_id in existing:
                if same(name, target_name) and is_near(point, item.point, view_based):
                    item.status = "existing"
                    item.existing_id = instance_id
                    break
        items.append(item)

to_place = [i for i in items if i.status == "place"]
already = [i for i in items if i.status == "existing"]
outside = [i for i in items if i.status == "outside"]
with_watts = [i for i in items if i.watts is not None]

# does the 2D family have somewhere to write the wattage?
probe_name = next((i.watt_name for i in with_watts), None)
# probe on an instance of the 2D family if one exists (instance parameters), else on its type
probe_instance = next((i for i in FilteredElementCollector(doc).OfClass(FamilyInstance)
                       if i.Symbol and i.Symbol.Family and i.Symbol.Family.Id == family.Id), None)
probe_param, probe_level = target_wattage_parameter(probe_instance, base_symbol, probe_name)
watt_note = (u"Wattage found on {} of {} linked fixture(s) (parameter '{}').".format(
    len(with_watts), len(items), probe_name) if with_watts else u"No wattage found on the linked fixtures.")


def watt_summary(key):
    values = sorted(set(i.watt_text for i in items if i.key == key and i.watt_text))
    return u", ".join(values) if values else u"-"


rows = []
for key in group_keys:
    kind, value = mapping[key]
    key_items = [i for i in items if i.key == key]
    rows.append({
        "linked": u"{} : {}".format(*key),
        "qty": len(key_items),
        "target": type_name(value) if kind == "existing" else value,
        "match": u"existing ({}%)".format(best_symbol(family_symbols, key)[1]) if kind == "existing" else u"NEW type",
        "watts": watt_summary(key),
        "new": len([i for i in key_items if i.status == "place"]),
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
                                     Orientation, CheckBox)
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import (SolidColorBrush, Brushes, Color, ColorConverter, FontFamily)
from System.Windows.Media.Media3D import (Viewport3D, ModelVisual3D, Model3DGroup, GeometryModel3D,
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
    "outside": Color.FromRgb(239, 68, 68),        # red   - not on the active view level
}


def item_color_key(item):
    if item.status == "existing":
        return "existing"
    if item.status in ("outside", "noloc"):
        return "outside"
    return "existing_type" if mapping[item.key][0] == "existing" else "new_type"


class PreviewWindow(object):
    def __init__(self):
        self.accepted = False
        self.copy_watts = True
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
                linked_doc.Title, family.Name, len(items), len(to_place), len(already),
                u", {} not on this view's level".format(len(outside)) if outside else u""),
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
        cancel = make_button("Cancel", False)
        cancel.IsCancel = True
        cancel.Click += lambda sender, args: window.Close()
        place = make_button(u"Place {} symbol(s)".format(len(to_place)) if to_place else "Update wattage only", True)
        place.IsEnabled = bool(to_place) or bool(already and with_watts)
        place.Click += self.on_place
        buttons.Children.Add(self.watt_box)
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
                           ("existing", "Already placed"), ("outside", "Not on view level")):
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
        points = [i.point for i in items if i.point is not None]
        if not points:
            return
        min_x = min(p.X for p in points); max_x = max(p.X for p in points)
        min_y = min(p.Y for p in points); max_y = max(p.Y for p in points)
        min_z = min(p.Z for p in points); max_z = max(p.Z for p in points)
        self.center = Point3D((min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2)
        self.radius = max(1.0, math.sqrt((max_x - min_x) ** 2 + (max_y - min_y) ** 2 + (max_z - min_z) ** 2) / 2)
        half = max(self.radius * 0.006, 0.3)

        meshes = dict((key, MeshGeometry3D()) for key in STATUS_COLORS)
        for item in items:
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
        widths = (2.4, 0.5, 2.0, 1.0, 1.2, 0.7)
        for width in widths:
            column = ColumnDefinition()
            column.Width = GridLength(width, GridUnitType.Star)
            grid.ColumnDefinitions.Add(column)
        headers = ("Linked fixture (Family : Type)", "Qty", "Placed as 2D type", "Match", "Wattage (link)", "To place")
        all_rows = [headers] + [(r["linked"], str(r["qty"]), r["target"], r["match"], r["watts"], str(r["new"]))
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
            u"2D family: {}   ({})".format(family.Name, "view-specific: placed in the active plan view"
                                          if view_based else "model family: placed at fixture height"),
            u"",
            u"{} linked lighting fixture(s) in {} type(s).".format(len(items), len(rows)),
            u"{} will be placed now.".format(len(to_place)),
            u"{} already have a symbol at that position (wattage is refreshed on those).".format(len(already)),
            u"{} use your existing types, {} new type(s) will be created.".format(
                sum(1 for k in group_keys if mapping[k][0] == "existing"),
                sum(1 for k in group_keys if mapping[k][0] == "new")),
        ]
        if outside:
            lines.append(u"{} skipped: not on the active view's level.".format(len(outside)))
        lines += [u"", watt_note]
        if with_watts:
            lines.append(u"Wattage is written to: {}".format(
                u"'{}' ({} parameter)".format(probe_param.Definition.Name, probe_level) if probe_param is not None
                else u"the placed symbol's own 'Wattage' instance parameter if it has one."))
        lines += [u"", u"Shift+Click the button to choose another link or 2D family.",
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
        self.window.Close()

    def show(self):
        self.window.ShowDialog()
        return self.accepted


preview = PreviewWindow()
if not preview.show():
    script.exit()
copy_watts = preview.copy_watts


# -----------------------------------------------------------------------------
# 8. Place + copy wattage (one transaction)
# -----------------------------------------------------------------------------
placed = 0
watt_written = 0
watt_missing_target = 0
created_types = []
failed = []
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


transaction = Transaction(doc, "Place 2D lighting symbols from link")
handling = transaction.GetFailureHandlingOptions()
handling.SetFailuresPreprocessor(WarningSwallower())
transaction.SetFailureHandlingOptions(handling)
transaction.Start()
try:
    used_lower = set(type_name(s).lower() for s in family_symbols)
    symbol_for = {}
    for key in group_keys:
        if not any(i.key == key and i.status == "place" for i in items):
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
                apply_watts(instance, item)
            except Exception as error:
                failed.append([u"{} : {}".format(*item.key), id_value(item.fixture.Id), u"wattage: {}".format(error)])

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
                instance = place_in_view(symbol, item.point)
            else:
                instance = place_in_model(link, transform, item.fixture, symbol, item.point, levels)
            location = instance.Location
            if rotate and abs(item.rotation) > 1e-6 and isinstance(location, LocationPoint) \
                    and not is_face_hosted(instance):
                origin = location.Point
                ElementTransformUtils.RotateElement(
                    doc, instance.Id, Line.CreateBound(origin, origin + XYZ.BasisZ), item.rotation)
            if write_comments:
                comments = instance.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if comments and not comments.IsReadOnly:
                    comments.Set(u"{} | ID {} | {}".format(linked_doc.Title, id_value(item.fixture.Id), source_label))
            apply_watts(instance, item)
            sub.Commit()
            placed += 1
            label = type_name(symbol)
            placed_per_type[label] = placed_per_type.get(label, 0) + 1
        except Exception as error:
            sub.RollBack()
            failed.append([source_label, id_value(item.fixture.Id), str(error)])

    if placed == 0 and not created_types and watt_written == 0:
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
                       columns=["2D type", "Placed"], title="Placed per type")
if created_types:
    output.print_table(table_data=[[n] for n in created_types], columns=["New type"], title="New types created")
if conflicts:
    output.print_table(table_data=conflicts, columns=["2D type", "Different wattages written"],
                       title="Wattage is a TYPE parameter but the linked fixtures differ - make it an instance parameter")
if failed:
    output.print_table(table_data=failed, columns=["Linked type", "Linked ID", "Reason"], title="Not placed")

summary = [
    u"{} symbol(s) placed.".format(placed),
    u"{} new type(s) created.".format(len(created_types)),
    u"{} already placed earlier (skipped).".format(len(already)),
]
if outside:
    summary.append(u"{} skipped: not on the active view's level.".format(len(outside)))
if copy_watts:
    summary.append(u"Wattage copied to {} symbol(s).".format(watt_written))
    if watt_missing_target:
        summary.append(u"{} symbol(s) have no wattage parameter - add an instance parameter "
                       u"'Wattage' to the 2D family.".format(watt_missing_target))
if failed:
    summary.append(u"{} problem(s) - see the output window.".format(len(failed)))
forms.alert(u"\n".join(summary) + u"\n\nOne Ctrl+Z undoes everything.", title="Light Placement 2D")
