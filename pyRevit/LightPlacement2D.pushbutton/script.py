# -*- coding: utf-8 -*-
__title__ = "LIGHT\nPLACEMENT 2D"
__doc__ = (
    "Place a 2D family you choose at every lighting fixture of a linked model. "
    "Each linked fixture type is matched to a type of the 2D family with the same "
    "name (types you already created are reused); missing types are created as new "
    "types. Review the matching, then symbols are placed at the centre of each "
    "linked fixture and rotated to match it. One Ctrl+Z undoes everything."
)

# =============================================================================
# LIGHT PLACEMENT 2D
#   1. Pick the linked model.
#   2. Pick YOUR 2D family (Lighting Fixture / Detail Item / Generic Annotation)
#      and the type new types are duplicated from.
#   3. Review the matching  linked "Family : Type"  ->  type of your 2D family.
#        EXISTING  = a type with the same name already exists - it is reused
#        NEW TYPE  = no match - a new type with the linked type name is created
#      Untick a row to skip it; you can also change the type used for any row.
#   4. Options: centre / insertion point, rotation, skip duplicates, Comments.
#   5. Everything is placed in ONE transaction (one Ctrl+Z undoes it).
#
# Model families (2D lighting fixture families) are placed at each fixture's
# height on the nearest level below. View-specific families (detail items,
# generic annotations) are placed in the ACTIVE PLAN VIEW.
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
from pyrevit import forms, script


uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
active_view = doc.ActiveView
output = script.get_output()

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
# 1. Linked model
# -----------------------------------------------------------------------------
links = []
for link_instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
    try:
        if link_instance.GetLinkDocument():
            links.append(link_instance)
    except Exception:
        pass
if not links:
    forms.alert("No loaded Revit links were found.\nLoad the linked model first (Manage > Manage Links).",
                exitscript=True)

link_options = sorted(
    [Option(u"{}   ({})".format(l.GetLinkDocument().Title, l.Name), l) for l in links],
    key=lambda option: option.name.lower())
picked = forms.SelectFromList.show(link_options, title="1. Select the linked model",
                                   name_attr="name", button_name="Next")
if not picked:
    script.exit()
link = picked.value
linked_doc = link.GetLinkDocument()
transform = link.GetTotalTransform()

# linked fixtures grouped by (Family, Type); nested sub-components skipped
groups = {}
for fixture in FilteredElementCollector(linked_doc) \
        .OfCategory(BuiltInCategory.OST_LightingFixtures) \
        .OfClass(FamilyInstance):
    if fixture.Symbol is None or fixture.SuperComponent is not None:
        continue
    key = (fixture.Symbol.FamilyName, type_name(fixture.Symbol))
    groups.setdefault(key, []).append(fixture)
if not groups:
    forms.alert("The selected link has no lighting fixtures.", exitscript=True)
group_keys = sorted(groups.keys(), key=lambda k: (k[0].lower(), k[1].lower()))


# -----------------------------------------------------------------------------
# 2. Your 2D family and the base type for new types
# -----------------------------------------------------------------------------
category_ids = set(int(c) for c in TARGET_CATEGORIES)
family_options = []
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
        family_options.append(Option(u"{}:  {}".format(category.Name, family.Name), (family, symbols)))
if not family_options:
    forms.alert("No Lighting Fixture, Detail Item or Generic Annotation families are loaded.\n"
                "Load your 2D family first (Insert > Load Family).", exitscript=True)
family_options.sort(key=lambda option: option.name.lower())

picked = forms.SelectFromList.show(family_options, title="2. Select YOUR 2D family (type to search)",
                                   name_attr="name", button_name="Next")
if not picked:
    script.exit()
family, family_symbols = picked.value
view_based = family.FamilyPlacementType == FamilyPlacementType.ViewBased
if view_based and not isinstance(active_view, ViewPlan):
    forms.alert("'{}' is a view-specific family (detail item / annotation).\n\n"
                "Open a floor or ceiling plan view first, then run the tool again.".format(family.Name),
                exitscript=True)

if len(family_symbols) == 1:
    base_symbol = family_symbols[0]
else:
    picked = forms.SelectFromList.show(
        [Option(type_name(s), s) for s in family_symbols],
        title="New types are duplicated from which type of '{}'?".format(family.Name),
        name_attr="name", button_name="Next")
    if not picked:
        script.exit()
    base_symbol = picked.value


# -----------------------------------------------------------------------------
# 3. Type matching (review)
# -----------------------------------------------------------------------------
# type names used by more than one linked family become "Family - Type"
type_to_families = {}
for fam_name, typ_name in group_keys:
    type_to_families.setdefault(typ_name.lower(), set()).add(fam_name)

mapping = {}   # key -> ("existing", symbol) | ("new", name) | ("skip", None)
for key in group_keys:
    fam_name, typ_name = key
    shared = len(type_to_families[typ_name.lower()]) > 1
    new_name = clean_name(u"{} - {}".format(fam_name, typ_name) if shared else typ_name)
    match = None
    for symbol in family_symbols:
        name = type_name(symbol)
        if same(name, typ_name) or same(name, new_name) or same(name, u"{} - {}".format(fam_name, typ_name)):
            match = symbol
            break
    mapping[key] = ("existing", match) if match else ("new", new_name)


def describe(key):
    kind, value = mapping[key]
    count = len(groups[key])
    label = u"{} : {}".format(key[0], key[1])
    if kind == "existing":
        return u"EXISTING   {}   ->   {}   ({} pcs)".format(label, type_name(value), count)
    if kind == "new":
        return u"NEW TYPE   {}   ->   + {}   ({} pcs)".format(label, value, count)
    return u"SKIP       {}   ({} pcs)".format(label, count)


while True:
    rows = [forms.TemplateListItem(Option(describe(key), key), checked=mapping[key][0] != "skip",
                                   name_attr="name")
            for key in group_keys]
    picked = forms.SelectFromList.show(
        rows, multiselect=True, name_attr="name", button_name="Accept matching",
        title="3. Review: ticked rows are placed (EXISTING = your type reused, NEW TYPE = created)")
    if picked is None:
        script.exit()
    ticked = set(option.value for option in picked)
    for key in group_keys:
        if key not in ticked:
            mapping[key] = ("skip", None)
        elif mapping[key][0] == "skip":            # re-ticked: back to name matching
            fam_name, typ_name = key
            new_name = clean_name(typ_name)
            match = next((s for s in family_symbols if same(type_name(s), typ_name)), None)
            mapping[key] = ("existing", match) if match else ("new", new_name)

    if not forms.alert("Do you want to change the type used for any linked type?",
                       yes=True, no=True, title="Review matching"):
        break
    to_change = forms.SelectFromList.show(
        [Option(describe(key), key) for key in group_keys],
        multiselect=True, name_attr="name", button_name="Change selected",
        title="Pick the linked types to change")
    for option in (to_change or []):
        key = option.value
        fam_name, typ_name = key
        choices = [Option(u"+ New type: {}".format(clean_name(typ_name)), ("new", clean_name(typ_name)))]
        choices += [Option(type_name(s), ("existing", s)) for s in family_symbols]
        choices.append(Option(u"(Skip - do not place)", ("skip", None)))
        choice = forms.SelectFromList.show(
            choices, name_attr="name", button_name="Use this type",
            title=u"Place '{} : {}' as".format(fam_name, typ_name))
        if choice:
            mapping[key] = choice.value

if all(mapping[key][0] == "skip" for key in group_keys):
    forms.alert("Every type is set to Skip - nothing to place.", exitscript=True)


# -----------------------------------------------------------------------------
# 4. Options
# -----------------------------------------------------------------------------
position = forms.CommandSwitchWindow.show(
    ["Centre of linked fixture", "Insertion point of linked fixture"],
    message="4. Place each symbol at:")
if not position:
    script.exit()
use_centre = position.startswith("Centre")

OPT_ROTATE = "Rotate symbols to match the linked fixtures"
OPT_DUPES = "Skip positions that already have the same symbol (within 50 mm)"
OPT_COMMENTS = "Write link name, linked ID and linked type into Comments"
OPT_LEVEL = "View-specific family: only fixtures on the active view's level"
option_names = [OPT_ROTATE, OPT_DUPES, OPT_COMMENTS] + ([OPT_LEVEL] if view_based else [])
picked = forms.SelectFromList.show(
    [forms.TemplateListItem(name, checked=True) for name in option_names],
    multiselect=True, title="Options (untick what you do not want)", button_name="Place symbols")
if picked is None:
    script.exit()
rotate = OPT_ROTATE in picked
skip_dupes = OPT_DUPES in picked
write_comments = OPT_COMMENTS in picked
only_view_level = OPT_LEVEL in picked


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

forms.alert("\n".join(summary) + "\n\nOne Ctrl+Z undoes everything.",
            title="Light Placement 2D")
