# -*- coding: utf-8 -*-
__title__ = "LIGHT\nPLACEMENT 2D"
__doc__ = (
    "AUTOMATIC: places your 2D lighting symbols at every lighting fixture of the "
    "linked model - no picking. The link, the 2D family and the type for each "
    "linked fixture are found automatically (types you already created are "
    "reused, missing ones are created). Shift+Click to change the remembered "
    "link / 2D family."
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
# 5. Place
# -----------------------------------------------------------------------------
levels = sorted(FilteredElementCollector(doc).OfClass(Level), key=lambda level: level.Elevation)
view_level = active_view.GenLevel if view_based else None

placed = 0
duplicates = 0
outside_level = 0
skipped = 0
created_types = []
failed = []
placed_per_type = {}

transaction = Transaction(doc, "Place 2D lighting symbols from link")
handling = transaction.GetFailureHandlingOptions()
handling.SetFailuresPreprocessor(WarningSwallower())
transaction.SetFailureHandlingOptions(handling)
transaction.Start()
try:
    # resolve mapping -> real symbols, creating new types
    used_lower = set(type_name(s).lower() for s in family_symbols)
    symbol_for = {}
    for key in group_keys:
        kind, value = mapping[key]
        if kind == "skip":
            skipped += len(groups[key])
        elif kind == "existing":
            symbol_for[key] = value
        else:
            try:
                name = unique_name(value, used_lower)
                symbol_for[key] = base_symbol.Duplicate(name)
                used_lower.add(name.lower())
                created_types.append(name)
            except Exception as error:
                failed.append([u"{} : {}".format(*key), "", u"New type '{}' not created: {}".format(value, error)])
                skipped += len(groups[key])

    for symbol in set(symbol_for.values()):
        if not symbol.IsActive:
            symbol.Activate()
    doc.Regenerate()

    # existing symbols of this family, for the duplicate check
    existing = []
    if skip_dupes:
        collector = FilteredElementCollector(doc, active_view.Id) if view_based else FilteredElementCollector(doc)
        for instance in collector.OfClass(FamilyInstance):
            symbol = instance.Symbol
            if symbol and symbol.Family and symbol.Family.Id == family.Id \
                    and isinstance(instance.Location, LocationPoint):
                existing.append((id_value(symbol.Id), instance.Location.Point))

    for key in group_keys:
        symbol = symbol_for.get(key)
        if symbol is None:
            continue
        symbol_id = id_value(symbol.Id)
        symbol_label = type_name(symbol)
        for fixture in groups[key]:
            source_label = u"{} : {}".format(*key)
            point, rotation = placement_of(fixture, transform, use_centre)
            if point is None:
                failed.append([source_label, id_value(fixture.Id), "No location"])
                continue
            if view_based and only_view_level and view_level is not None:
                fixture_level = level_for(point.Z, levels)
                if fixture_level is None or fixture_level.Id != view_level.Id:
                    outside_level += 1
                    continue
            if skip_dupes and any(sid == symbol_id and is_near(p, point, view_based) for sid, p in existing):
                duplicates += 1
                continue

            sub = SubTransaction(doc)
            sub.Start()
            try:
                if view_based:
                    instance = place_in_view(symbol, point)
                else:
                    instance = place_in_model(link, transform, fixture, symbol, point, levels)
                location = instance.Location
                if rotate and abs(rotation) > 1e-6 and isinstance(location, LocationPoint) \
                        and not is_face_hosted(instance):
                    origin = location.Point
                    ElementTransformUtils.RotateElement(
                        doc, instance.Id, Line.CreateBound(origin, origin + XYZ.BasisZ), rotation)
                if write_comments:
                    comments = instance.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    if comments and not comments.IsReadOnly:
                        comments.Set(u"{} | ID {} | {}".format(linked_doc.Title, id_value(fixture.Id), source_label))
                sub.Commit()
                existing.append((symbol_id, point))
                placed += 1
                placed_per_type[symbol_label] = placed_per_type.get(symbol_label, 0) + 1
            except Exception as error:
                sub.RollBack()
                failed.append([source_label, id_value(fixture.Id), str(error)])

    if placed == 0 and not created_types:
        transaction.RollBack()
    else:
        transaction.Commit()
except Exception:
    if transaction.HasStarted() and not transaction.HasEnded():
        transaction.RollBack()
    raise


# -----------------------------------------------------------------------------
# 6. Report
# -----------------------------------------------------------------------------
summary = [
    "{} symbol(s) placed.".format(placed),
    "{} new type(s) created.".format(len(created_types)),
]
if duplicates:
    summary.append("{} skipped: same symbol already at that position.".format(duplicates))
if outside_level:
    summary.append("{} skipped: fixture not on the active view's level.".format(outside_level))
if skipped:
    summary.append("{} skipped: type set to Skip.".format(skipped))
if failed:
    summary.append("{} could not be placed - see the output window.".format(len(failed)))

output.print_md("**Link:** {}  |  **2D family:** {}".format(linked_doc.Title, family.Name))
output.print_table(table_data=match_rows,
                   columns=["Linked fixture (Family : Type)", "Qty", "Placed as 2D type", "Match"],
                   title="Type matching")
if placed_per_type:
    output.print_table(
        table_data=[[name, count] for name, count in sorted(placed_per_type.items())],
        columns=["2D type", "Placed"],
        title="Placed per type")
if created_types:
    output.print_table(table_data=[[name] for name in created_types],
                       columns=["New type"], title="New types created")
if failed:
    output.print_table(table_data=failed, columns=["Linked type", "Linked ID", "Reason"],
                       title="Not placed")

forms.alert("\n".join(summary) + "\n\nLink: {}\n2D family: {}\n\nThe output window shows which 2D type each linked type got.\n"
            "One Ctrl+Z undoes everything. Shift+Click to change link / family.".format(linked_doc.Title, family.Name),
            title="Light Placement 2D")
