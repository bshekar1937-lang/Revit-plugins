# -*- coding: utf-8 -*-
__title__ = "Cable Tray Auto Route Pro"
__doc__ = (
    "Create multi-size, multi-tier cable trays from selected detail lines. "
    "Revit inserts and sizes the tee at every T-junction; lanes that must "
    "cross other lanes are offset up or down by the height set in the "
    "dialog. Unworkable dimensions are detected and workable values are "
    "suggested for confirmation before anything is created. An optional "
    "Comments text - with tokens such as {size}, {tier} and {level} - is "
    "written on every tray and fitting the tool creates. A tabbed preview "
    "lists every clash with existing services - linked models included - "
    "before anything is placed."
)

# =============================================================================
# CABLE TRAY AUTO ROUTE PRO  -  clash-free tee junctions
# -----------------------------------------------------------------------------
# What it does
#   1. A dialog collects the tray type, tee fitting, sizes, spacing, tiers and
#      the tee crossing options (direction up/down, offset step, bend angle,
#      clearance).
#   2. You pick straight detail lines. Each line becomes one parallel lane per
#      tray size, repeated for every vertical tier. The 1st tier sits at the
#      chosen level's FFL + offset (measured to bottom / centre / top of
#      tray); further tiers stack upwards by the tier gap.
#      Lines that meet end to end get a corner bend (any angle) or a straight
#      join; connected lines are run in one direction internally so every
#      tray size stays on the same side round a corner (nested bends).
#   3. FITTING CHECK: the real tee and vertical bend fittings are inserted on
#      throw-away trays inside a transaction that is ALWAYS rolled back, so
#      their true sizes are known. If an entered dimension cannot work (offset
#      step, clearance, spacing, tier spacing, bend angle, line length, tee or
#      bend family), workable values are suggested and you confirm them.
#   4. TEE CLASH AVOIDANCE: lanes are ranked by their distance from the side
#      the branch arrives from:
#         rank 0 (nearest)  -> stays level, plain tee
#         rank 1            -> branch AND its main lane offset 1 x step
#         rank 2            -> branch AND its main lane offset 2 x step ...
#      Crossing branches are always one step clear of every main lane they
#      pass, and each tee stays level.
#   5. Tees are inserted with Revit's NewTeeFitting, so Revit picks the tray
#      type's Tee family and SIZES IT to the trays automatically.
#   6. A tabbed clash PREVIEW is shown BEFORE anything is created:
#        - Overview        : what will be created, tee by tee
#        - Existing services: every bounding-box overlap with ducts,
#          pipes, conduit and other cable trays - in this model and, if
#          asked, in the linked models. Each row can be selected or
#          zoomed to in Revit, and the check can be re-run with a
#          different clearance without leaving the window.
#        - Tray crossings  : planned trays that cross each other
#          without enough vertical separation.
#      From there you can create, go back to the settings, or cancel.
#   7. COMMENTS: one text - with tokens such as {part}, {size}, {tier},
#      {line} and {level} - is written into the built-in Comments
#      parameter of every tray, and of the fittings too if asked, so the
#      new work can be filtered, scheduled and tagged at once. Existing
#      text is either replaced or added to (your choice in the dialog).
#
# MODIFIES THE MODEL - creates cable trays and fittings in one transaction and
# may set the tray type's Tee / Vertical Bend fittings (you are told first).
# The Comments parameter is only ever written on elements this tool creates.
# A single Undo (Ctrl+Z) removes everything the tool created.
# Revit stores lengths in decimal feet; every value in the dialog is in mm and
# is converted with mm_to_internal().
#
# Revit versions: 2023, 2024, 2025, 2026, 2027 (pyRevit, IronPython engine).
#   - Element ids are read through element_id_value(): ElementId.IntegerValue
#     was removed in Revit 2026 (ElementId.Value exists from 2024).
#   - Units use UnitTypeId (available from Revit 2021).
# =============================================================================

import datetime
import math

from Autodesk.Revit import DB
from Autodesk.Revit.DB.Electrical import CableTray, CableTrayType
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from pyrevit import forms, revit, script

# WPF classes used to build the tray-size rows of the dialog in code.
import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System.Collections.Generic import List
from System.Windows import (
    Clipboard, GridLength, GridUnitType, SystemParameters, Thickness
)
from System.Windows.Controls import (
    Border, Button, ColumnDefinition, Grid, RowDefinition, StackPanel,
    TextBlock, TextBox
)


doc = revit.doc
uidoc = revit.uidoc

TITLE = "Cable Tray Auto Route Pro"
EPSILON = 1e-9
MIN_STRAIGHT_MM = 50.0      # straight tray always kept between two fittings
ROUND_TO_MM = 25.0          # suggested values are rounded up to this
PROBE_LENGTH_MM = 3000.0    # length of the throw-away probe trays


class RouteGeometryError(Exception):
    """A planned tray route does not fit (line too short, overlapping
    offsets...). Shown as a plain, fixable message - never a traceback."""

    def __init__(self, message, line_index=None, shortfall_mm=0.0,
                 side=None):
        Exception.__init__(self, message)
        self.line_index = line_index
        self.shortfall_mm = shortfall_mm
        self.side = side        # 'start' / 'end' of the detail line, if known
        self.parts = None       # all problems found on the same lane


# =============================================================================
# Names, units, ids
# =============================================================================
def tray_type_name(tray_type):
    parameter = tray_type.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
    if parameter is None or not parameter.AsString():
        raise Exception("Could not read a cable tray type name.")
    return parameter.AsString()


def fitting_type_name(fitting_type):
    parameter = fitting_type.get_Parameter(
        DB.BuiltInParameter.SYMBOL_NAME_PARAM
    )
    if parameter is None or not parameter.AsString():
        return fitting_type.Family.Name
    return "{} : {}".format(fitting_type.Family.Name, parameter.AsString())


def mm_to_internal(value):
    """Millimetres -> Revit internal feet."""
    try:
        unit_id = DB.UnitTypeId.Millimeters
    except AttributeError:
        return DB.UnitUtils.ConvertToInternalUnits(
            value, DB.DisplayUnitType.DUT_MILLIMETERS
        )
    return DB.UnitUtils.ConvertToInternalUnits(value, unit_id)


def internal_to_mm(value):
    """Revit internal feet -> millimetres."""
    return value / mm_to_internal(1.0)


def element_id_value(element_id):
    """ElementId -> integer on every Revit version (2023 - 2027).
    Revit 2024+ has ElementId.Value; IntegerValue was REMOVED in 2026.
    Revit 2023 only has IntegerValue, used as the fallback."""
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def round_up_mm(value, increment=ROUND_TO_MM):
    """Round a suggested dimension up to a tidy value (e.g. 25 mm)."""
    return math.ceil(value / increment - 1e-6) * increment


def set_tray_dimension(tray, parameter_id, value, label):
    parameter = tray.get_Parameter(parameter_id)
    if parameter is None or parameter.IsReadOnly:
        raise Exception(
            "The selected tray type does not allow {} changes.".format(label)
        )
    parameter.Set(mm_to_internal(value))


# =============================================================================
# Plan geometry helpers
# =============================================================================
def plan_vector(start, end):
    """Vector from start to end with the height difference removed."""
    return DB.XYZ(end.X - start.X, end.Y - start.Y, 0.0)


def horizontal_unit(start, end):
    vector = plan_vector(start, end)
    if vector.GetLength() <= EPSILON:
        raise RouteGeometryError(
            "A selected detail line has no length in plan view."
        )
    return vector.Normalize()


def plan_normal(direction):
    """Right-hand normal used for every lane offset (kept consistent)."""
    return DB.XYZ(direction.Y, -direction.X, 0.0).Normalize()


def lane_offset(lane_index, lane_count, spacing):
    """Sideways offset of a lane, centred on the detail line."""
    return (lane_index - (lane_count - 1) / 2.0) * spacing


DIRECTION_NAMES = {
    "up": "up (rise over)",
    "down": "down (drop under)",
    "alternate": "up and down alternately",
}


def lane_lift(rank, mode, step):
    """Vertical offset of a lane at a tee, by its crossing rank.

    up        : 0, +1, +2, +3 ... steps
    down      : 0, -1, -2, -3 ... steps
    alternate : 0, +1, -1, +2, -2 ... steps  (best for 4+ parallel trays)

    Every scheme keeps a crossing branch at least one step clear of every
    main lane it passes - including the main lane's own offset bends,
    because each new level lies outside the range already used on that
    side (0..+k or -k..0)."""
    if rank <= 0:
        return 0.0
    if mode == "down":
        return -rank * step
    if mode == "alternate":
        level = (rank + 1) // 2
        return level * step if rank % 2 == 1 else -level * step
    return rank * step


def lift_extent(lane_count, mode, step):
    """(highest rise, deepest drop) used at a tee, both as positive values."""
    lifts = [lane_lift(rank, mode, step) for rank in range(lane_count)]
    return abs(max(lifts + [0.0])), abs(min(lifts + [0.0]))


def describe_extent(rise, drop):
    """'+500 mm', '-500 mm' or '+450 / -450 mm' for messages."""
    if drop < 0.5:
        return "+{:g} mm".format(rise)
    if rise < 0.5:
        return "-{:g} mm".format(drop)
    return "+{:g} / -{:g} mm".format(rise, drop)


def line_intersection_2d(first_start, first_end, second_start, second_end):
    first_dx = first_end.X - first_start.X
    first_dy = first_end.Y - first_start.Y
    second_dx = second_end.X - second_start.X
    second_dy = second_end.Y - second_start.Y
    denominator = first_dx * second_dy - first_dy * second_dx
    if abs(denominator) < 1e-9:
        return None
    between_x = second_start.X - first_start.X
    between_y = second_start.Y - first_start.Y
    first_parameter = (
        between_x * second_dy - between_y * second_dx
    ) / denominator
    second_parameter = (
        between_x * first_dy - between_y * first_dx
    ) / denominator
    point = DB.XYZ(
        first_start.X + first_parameter * first_dx,
        first_start.Y + first_parameter * first_dy,
        first_start.Z
    )
    return point, first_parameter, second_parameter


def endpoint_links(line_segments):
    """{(line, end): [(other_line, other_end), ...]} - which detail-line ends
    touch other detail-line ENDS in plan (end 0 = start, 1 = end)."""
    tolerance = mm_to_internal(2.0)
    links = {}
    for index, segment in enumerate(line_segments):
        for end in (0, 1):
            partners = []
            for other_index, other in enumerate(line_segments):
                if other_index == index:
                    continue
                for other_end in (0, 1):
                    if (plan_vector(segment[end], other[other_end]).GetLength()
                            <= tolerance):
                        partners.append((other_index, other_end))
            links[(index, end)] = partners
    return links


def orient_line_chains(line_segments):
    """Flip detail lines so that every corner / straight join runs
    end -> start. Lane offsets are measured to the right of each line, so a
    consistent direction keeps each tray size on the same side all the way
    round a corner (nested bends, no lanes crossing). Returns the oriented
    segments and a 'flipped' flag per line."""
    links = endpoint_links(line_segments)
    flipped = [None] * len(line_segments)
    for root in range(len(line_segments)):
        if flipped[root] is not None:
            continue
        flipped[root] = False
        stack = [root]
        while stack:
            line = stack.pop()
            for end in (0, 1):
                partners = links[(line, end)]
                if len(partners) != 1:
                    continue        # free end, or 3+ lines meet here
                other, other_end = partners[0]
                if len(links[(other, other_end)]) != 1:
                    continue
                effective_end = end if not flipped[line] else 1 - end
                wanted_other_end = 1 - effective_end
                if flipped[other] is None:
                    flipped[other] = other_end != wanted_other_end
                    stack.append(other)
    oriented = []
    for index, (start, end) in enumerate(line_segments):
        oriented.append((end, start) if flipped[index] else (start, end))
    return oriented, [bool(flag) for flag in flipped]


def adjusted_route_ends(line_index, line_segments, lane_index,
                        run_spacing, lane_count, links):
    """Offset a detail line to its lane. Where the line meets ONE other line
    end-to-end, stretch / trim the lane so it meets the same lane of the
    other line exactly - that is where the corner bend is inserted.
    Works for any corner angle (not only 90 deg) and any lane offset.

    Returns (start, end, start_link, end_link); a link is None (free end),
    'bend' (corner) or 'join' (straight continuation)."""
    start, end = line_segments[line_index]
    direction = horizontal_unit(start, end)
    offset_distance = lane_offset(lane_index, lane_count, run_spacing)
    offset = plan_normal(direction).Multiply(offset_distance)
    points = [start + offset, end + offset]
    kinds = [None, None]

    for my_end in (0, 1):
        partners = links.get((line_index, my_end), [])
        if len(partners) != 1:
            continue                # free end, or 3+ lines meet (no bend)
        other_index, other_end = partners[0]
        other_start, other_finish = line_segments[other_index]
        other_direction = horizontal_unit(other_start, other_finish)
        alignment = abs(direction.DotProduct(other_direction))
        if alignment > 0.995:
            kinds[my_end] = "join"  # straight on: lanes already line up
            continue
        if alignment > 0.97:
            continue                # hairpin - no sensible bend
        other_offset = plan_normal(other_direction).Multiply(offset_distance)
        hit = line_intersection_2d(
            start + offset, end + offset,
            other_start + other_offset, other_finish + other_offset
        )
        if hit is None:
            continue
        points[my_end] = hit[0]
        kinds[my_end] = "bend"
    return points[0], points[1], kinds[0], kinds[1]


def level_elevation(level):
    """Level height in model coordinates (ignores the Elevation Base
    setting, so it matches the Z of elements placed on that level)."""
    try:
        return level.ProjectElevation
    except AttributeError:
        return level.Elevation


REFERENCE_NAMES = {
    "bottom": "bottom of tray",
    "centre": "centre of tray",
    "top": "top of tray",
}


def reference_shift_mm(reference, height):
    """Tray centreline above the FFL reference point (mm)."""
    if reference == "bottom":
        return height / 2.0
    if reference == "top":
        return -height / 2.0
    return 0.0


def tee_junctions(line_segments):
    """Find detail lines whose END touches the MIDDLE of another line."""
    junctions = []
    tolerance = mm_to_internal(2)
    minimum_main_inset = mm_to_internal(300)
    for branch_index, (branch_start, branch_end) in enumerate(line_segments):
        for main_index, (main_start, main_end) in enumerate(line_segments):
            if branch_index == main_index:
                continue
            branch_direction = (branch_end - branch_start).Normalize()
            main_direction = (main_end - main_start).Normalize()
            if abs(branch_direction.DotProduct(main_direction)) > 0.05:
                continue
            crossing = line_intersection_2d(
                branch_start, branch_end, main_start, main_end
            )
            if crossing is None:
                continue
            point, _, main_parameter = crossing
            branch_start_distance = point.DistanceTo(branch_start)
            branch_end_distance = point.DistanceTo(branch_end)
            if min(branch_start_distance, branch_end_distance) > tolerance:
                continue
            main_length = main_start.DistanceTo(main_end)
            if min(
                main_parameter * main_length,
                (1.0 - main_parameter) * main_length
            ) < minimum_main_inset:
                continue
            junctions.append({
                "branch": branch_index,
                "branch_endpoint": (
                    0 if branch_start_distance <= branch_end_distance else 1
                ),
                "main": main_index,
                "point": point,
            })

    unique = []
    for candidate in junctions:
        if any(
            item["branch"] == candidate["branch"]
            and item["branch_endpoint"] == candidate["branch_endpoint"]
            and item["main"] == candidate["main"]
            and item["point"].DistanceTo(candidate["point"]) <= tolerance
            for item in unique
        ):
            continue
        unique.append(candidate)
    return unique


# =============================================================================
# Clash checks (read-only)
# =============================================================================
# Existing services the planned trays are checked against.
SERVICE_CATEGORIES = (
    DB.BuiltInCategory.OST_CableTray,
    DB.BuiltInCategory.OST_CableTrayFitting,
    DB.BuiltInCategory.OST_Conduit,
    DB.BuiltInCategory.OST_ConduitFitting,
    DB.BuiltInCategory.OST_DuctCurves,
    DB.BuiltInCategory.OST_DuctFitting,
    DB.BuiltInCategory.OST_FlexDuctCurves,
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_PipeFitting,
    DB.BuiltInCategory.OST_FlexPipeCurves,
)

DEFAULT_SERVICE_CLEARANCE_MM = 1.0   # the preview starts hard up against


def element_type_name(element):
    """Family / type name of an existing element, for the clash list."""
    for getter in ("Name",):
        try:
            name = getattr(element, getter)
            if name:
                return name
        except Exception:
            pass
    try:
        symbol = doc.GetElement(element.GetTypeId())
        if symbol is not None:
            return fitting_type_name(symbol)
    except Exception:
        pass
    return "(unnamed)"


def element_category_name(element):
    try:
        return element.Category.Name if element.Category else "Service"
    except Exception:
        return "Service"


def transformed_bounds(transform, minimum, maximum):
    """Axis-aligned bounds of a linked box brought into host coordinates."""
    corners = [
        transform.OfPoint(DB.XYZ(x, y, z))
        for x in (minimum.X, maximum.X)
        for y in (minimum.Y, maximum.Y)
        for z in (minimum.Z, maximum.Z)
    ]
    return (
        DB.XYZ(min(point.X for point in corners),
               min(point.Y for point in corners),
               min(point.Z for point in corners)),
        DB.XYZ(max(point.X for point in corners),
               max(point.Y for point in corners),
               max(point.Z for point in corners)),
    )


def collect_service_boxes(source_doc, link_name=None, transform=None):
    """Bounding boxes of the services in one document, host coordinates."""
    boxes = []
    seen_ids = set()
    for category in SERVICE_CATEGORIES:
        try:
            elements = (
                DB.FilteredElementCollector(source_doc)
                .OfCategory(category)
                .WhereElementIsNotElementType()
                .ToElements()
            )
        except Exception:
            continue        # category not present in that document
        for element in elements:
            element_id = element_id_value(element.Id)
            if element_id in seen_ids:
                continue
            seen_ids.add(element_id)
            try:
                box = element.get_BoundingBox(None)
            except Exception:
                box = None
            if box is None:
                continue
            minimum, maximum = box.Min, box.Max
            if transform is not None and not transform.IsIdentity:
                minimum, maximum = transformed_bounds(
                    transform, minimum, maximum
                )
            boxes.append({
                "element": element,
                "id": element_id,
                "min": minimum,
                "max": maximum,
                "link": link_name,
                "category": element_category_name(element),
                "name": element_type_name(element),
            })
    return boxes


def service_boxes(include_links=False):
    """Every existing service to check against, in host coordinates.

    Linked models hold most other disciplines, so they can be included -
    their boxes are transformed by the link's own position."""
    boxes = collect_service_boxes(doc)
    if not include_links:
        return boxes
    try:
        link_instances = (
            DB.FilteredElementCollector(doc)
            .OfClass(DB.RevitLinkInstance)
            .ToElements()
        )
    except Exception:
        return boxes
    for link_instance in link_instances:
        try:
            link_doc = link_instance.GetLinkDocument()
        except Exception:
            link_doc = None
        if link_doc is None:
            continue        # not loaded
        try:
            transform = link_instance.GetTotalTransform()
        except Exception:
            transform = None
        try:
            link_name = link_instance.Name
        except Exception:
            link_name = "linked model"
        boxes.extend(
            collect_service_boxes(link_doc, link_name, transform)
        )
    return boxes


def tray_bounds(proposed, clearance):
    """Box of a planned tray piece, grown by its size and the clearance."""
    start = proposed["start"]
    end = proposed["end"]
    half_width = mm_to_internal(proposed["width"]) / 2.0
    half_height = mm_to_internal(proposed["height"]) / 2.0
    return (
        DB.XYZ(min(start.X, end.X) - half_width - clearance,
               min(start.Y, end.Y) - half_width - clearance,
               min(start.Z, end.Z) - half_height - clearance),
        DB.XYZ(max(start.X, end.X) + half_width + clearance,
               max(start.Y, end.Y) + half_width + clearance,
               max(start.Z, end.Z) + half_height + clearance),
    )


def box_overlap_mm(first_min, first_max, second_min, second_max):
    """Overlap of two boxes per axis in mm, or None when they are clear."""
    overlap = []
    for low, high, other_low, other_high in (
            (first_min.X, first_max.X, second_min.X, second_max.X),
            (first_min.Y, first_max.Y, second_min.Y, second_max.Y),
            (first_min.Z, first_max.Z, second_min.Z, second_max.Z)):
        depth = min(high, other_high) - max(low, other_low)
        if depth < 0.0:
            return None
        overlap.append(internal_to_mm(depth))
    return tuple(overlap)


def service_clashes(proposed_trays,
                    clearance_mm=DEFAULT_SERVICE_CLEARANCE_MM,
                    include_links=False, boxes=None):
    """Bounding-box clashes of the planned trays with existing services.

    One record per existing element, with every tray piece that hits it and
    the deepest overlap, so the preview lists services rather than pairs."""
    if boxes is None:
        boxes = service_boxes(include_links)
    clearance = mm_to_internal(clearance_mm)
    found = {}
    order = []
    for proposed in proposed_trays:
        tray_min, tray_max = tray_bounds(proposed, clearance)
        for service in boxes:
            overlap = box_overlap_mm(
                tray_min, tray_max, service["min"], service["max"]
            )
            if overlap is None:
                continue
            key = (service["link"], service["id"])
            record = found.get(key)
            if record is None:
                record = dict(service)
                record["pieces"] = []
                record["overlap"] = overlap
                found[key] = record
                order.append(key)
            record["pieces"].append(proposed)
            if min(overlap) > min(record["overlap"]):
                record["overlap"] = overlap
    clashes = [found[key] for key in order]
    # Worst first: the deepest overlap is the one worth looking at.
    clashes.sort(key=lambda item: min(item["overlap"]), reverse=True)
    return clashes


def describe_overlap(overlap):
    """'120 x 45 x 80 mm' for a clash record's overlap."""
    return "{:.0f} x {:.0f} x {:.0f} mm".format(*overlap)


def internal_crossings(segments):
    """Planned trays whose centrelines cross in plan WITHOUT enough vertical
    separation - i.e. they would physically clash with each other."""
    tolerance = mm_to_internal(5.0)
    lengths = []
    boxes = []
    for item in segments:
        lengths.append(plan_vector(item["start"], item["end"]).GetLength())
        boxes.append((
            min(item["start"].X, item["end"].X),
            max(item["start"].X, item["end"].X),
            min(item["start"].Y, item["end"].Y),
            max(item["start"].Y, item["end"].Y),
        ))
    problems = []
    for first_index in range(len(segments)):
        first = segments[first_index]
        first_length = lengths[first_index]
        if first_length <= tolerance:
            continue    # vertical riser: no line in plan to cross
        first_box = boxes[first_index]
        for second_index in range(first_index + 1, len(segments)):
            second = segments[second_index]
            second_length = lengths[second_index]
            if (second_length <= tolerance
                    or first["route_key"] == second["route_key"]):
                continue
            second_box = boxes[second_index]
            if (first_box[1] < second_box[0] or second_box[1] < first_box[0]
                    or first_box[3] < second_box[2]
                    or second_box[3] < first_box[2]):
                continue    # quick reject: plan extents do not overlap
            hit = line_intersection_2d(
                first["start"], first["end"], second["start"], second["end"]
            )
            if hit is None:
                continue    # parallel lanes
            _, first_t, second_t = hit
            first_margin = tolerance / first_length
            second_margin = tolerance / second_length
            if not (first_margin < first_t < 1.0 - first_margin
                    and second_margin < second_t < 1.0 - second_margin):
                continue    # only touching at an end (corner / fitting)
            first_z = first["start"].Z + first_t * (
                first["end"].Z - first["start"].Z
            )
            second_z = second["start"].Z + second_t * (
                second["end"].Z - second["start"].Z
            )
            needed = mm_to_internal(
                (first["height"] + second["height"]) / 2.0
            )
            separation = abs(first_z - second_z)
            if separation < needed - mm_to_internal(1.0):
                problems.append((first, second, separation, needed))
    return problems


def describe_segment(segment):
    return "line {} ({:g}x{:g} mm, tier {})".format(
        segment["line_index"] + 1,
        segment["width"],
        segment["height"],
        segment["tier_index"] + 1
    )


# =============================================================================
# Fittings assigned to the cable tray TYPE (Revit uses these automatically)
# =============================================================================
FITTING_ROLES = {
    # role: (built-in parameter names to try, parameter names to try)
    "tee": (("RBS_CURVETYPE_DEFAULT_TEE_PARAM",), ("Tee",)),
    "inside_bend": ((), ("Vertical Inside Bend",)),
    "outside_bend": ((), ("Vertical Outside Bend",)),
    "horizontal_bend": (("RBS_CURVETYPE_DEFAULT_HORIZONTAL_BEND_PARAM",),
                        ("Horizontal Bend",)),
}


def type_fitting_parameter(tray_type, role):
    """The tray type parameter that holds the fitting for this role."""
    builtin_names, names = FITTING_ROLES[role]
    for builtin_name in builtin_names:
        builtin = getattr(DB.BuiltInParameter, builtin_name, None)
        if builtin is None:
            continue
        try:
            parameter = tray_type.get_Parameter(builtin)
        except Exception:
            parameter = None
        if (parameter is not None
                and parameter.StorageType == DB.StorageType.ElementId):
            return parameter
    for name in names:
        parameter = tray_type.LookupParameter(name)
        if (parameter is not None
                and parameter.StorageType == DB.StorageType.ElementId):
            return parameter
    return None


def assigned_fitting(tray_type, role):
    parameter = type_fitting_parameter(tray_type, role)
    if parameter is None:
        return None
    element = doc.GetElement(parameter.AsElementId())
    return element if isinstance(element, DB.FamilySymbol) else None


def assign_fitting(tray_type, role, symbol):
    """Set the tray type's fitting for a role. Must run inside a
    transaction. Returns True when the type was changed."""
    if symbol is None:
        return False
    parameter = type_fitting_parameter(tray_type, role)
    if parameter is None or parameter.IsReadOnly:
        return False
    if element_id_value(parameter.AsElementId()) == element_id_value(symbol.Id):
        return False
    if not symbol.IsActive:
        symbol.Activate()
    parameter.Set(symbol.Id)
    return True


def family_prefix(symbol):
    """'M_Channel Horizontal Tee' -> 'm_channel' (used to match families)."""
    return symbol.Family.Name.strip().split(" ")[0].lower()


def build_tee_options(fitting_symbols, tray_types):
    """Loaded cable-tray tee types (plus any tee already on a tray type)."""
    options = []
    seen = set()
    for symbol in fitting_symbols:
        if "tee" in fitting_type_name(symbol).lower():
            options.append(symbol)
            seen.add(element_id_value(symbol.Id))
    for tray_type in tray_types:
        symbol = assigned_fitting(tray_type, "tee")
        if symbol is not None and element_id_value(symbol.Id) not in seen:
            options.append(symbol)
            seen.add(element_id_value(symbol.Id))
    options.sort(key=lambda symbol: (
        0 if "horizontal" in fitting_type_name(symbol).lower() else 1,
        fitting_type_name(symbol).lower()
    ))
    return options


def bend_pair_candidates(prefix):
    """Loaded (Vertical Inside Bend, Vertical Outside Bend) pairs of the same
    family style, preferring the style of the selected tee."""
    inside = [
        symbol for symbol in fitting_symbols
        if "vertical inside bend" in symbol.Family.Name.lower()
    ]
    outside = [
        symbol for symbol in fitting_symbols
        if "vertical outside bend" in symbol.Family.Name.lower()
    ]

    def preference(symbol):
        return (
            0 if family_prefix(symbol) == prefix else 1,
            fitting_type_name(symbol).lower()
        )

    pairs = []
    for inside_symbol in sorted(inside, key=preference):
        for outside_symbol in sorted(outside, key=preference):
            if family_prefix(inside_symbol) == family_prefix(outside_symbol):
                pairs.append((inside_symbol, outside_symbol))
                break
    return pairs[:4]


def horizontal_bend_candidates(prefix):
    """Loaded Horizontal Bend types, preferring the style of the tee."""
    found = [
        symbol for symbol in fitting_symbols
        if "horizontal bend" in symbol.Family.Name.lower()
    ]
    found.sort(key=lambda symbol: (
        0 if family_prefix(symbol) == prefix else 1,
        fitting_type_name(symbol).lower()
    ))
    return found[:4]


# =============================================================================
# Tray / connector helpers
# =============================================================================
def fitting_cable_tray_connectors(fitting):
    model = fitting.MEPModel
    if model is None:
        return []
    return [
        connector
        for connector in model.ConnectorManager.Connectors
        if connector.Domain == DB.Domain.DomainCableTrayConduit
        and connector.ConnectorType == DB.ConnectorType.End
    ]


def end_connectors(element):
    return [
        connector for connector in element.ConnectorManager.Connectors
        if connector.ConnectorType == DB.ConnectorType.End
    ]


def nearest_free_connector(element, point):
    free = [
        connector for connector in end_connectors(element)
        if not connector.IsConnected
    ]
    if not free:
        return None
    return min(free, key=lambda connector: connector.Origin.DistanceTo(point))


def connectors_at(segments, point, tolerance):
    """Free tray connectors of the given segments lying at a point."""
    found = []
    for segment in segments:
        for connector in end_connectors(segment["element"]):
            if (not connector.IsConnected
                    and connector.Origin.DistanceTo(point) <= tolerance):
                found.append(connector)
    return found


def create_tray(tray_type, level, start, end, width, height):
    tray = CableTray.Create(doc, tray_type.Id, start, end, level.Id)
    set_tray_dimension(
        tray, DB.BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM, width, "width"
    )
    set_tray_dimension(
        tray, DB.BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM, height, "height"
    )
    return tray


def is_vertical_segment(segment):
    return (
        plan_vector(segment["start"], segment["end"]).GetLength()
        <= mm_to_internal(1.0)
        and abs(segment["end"].Z - segment["start"].Z) > mm_to_internal(1.0)
    )


def orient_vertical_tray(tray, travel_direction):
    """A vertical riser (90 deg bends) has no natural orientation. Spin it
    about its own axis so its open face continues the horizontal trays it
    joins. Returns True when the riser was oriented."""
    try:
        curve = tray.Location.Curve
        first_point = curve.GetEndPoint(0)
        second_point = curve.GetEndPoint(1)
        axis_direction = (second_point - first_point).Normalize()
        current_up = tray.CurveNormal
        if axis_direction.Z > 0:
            target_up = travel_direction.Negate()
        else:
            target_up = travel_direction
        angle = math.atan2(
            axis_direction.DotProduct(current_up.CrossProduct(target_up)),
            current_up.DotProduct(target_up)
        )
        if abs(angle) > 1e-6:
            DB.ElementTransformUtils.RotateElement(
                doc, tray.Id,
                DB.Line.CreateBound(first_point, second_point),
                angle
            )
        return True
    except Exception:
        return False


# =============================================================================
# FITTING PROBES - measure the real fittings (always rolled back)
# =============================================================================
def probe_tee(tray_type, level, origin, width, height):
    """Build a throw-away T of three trays, let Revit insert and size the
    tray type's tee, and measure how far it reaches along the main and the
    branch. Rolled back immediately - nothing is kept."""
    length = mm_to_internal(PROBE_LENGTH_MM)
    along = DB.XYZ.BasisX
    across = DB.XYZ.BasisY
    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        first = create_tray(tray_type, level, origin - along.Multiply(length),
                            origin, width, height)
        second = create_tray(tray_type, level, origin,
                             origin + along.Multiply(length), width, height)
        branch = create_tray(tray_type, level, origin + across.Multiply(length),
                             origin, width, height)
        doc.Regenerate()
        tee = doc.Create.NewTeeFitting(
            nearest_free_connector(first, origin),
            nearest_free_connector(second, origin),
            nearest_free_connector(branch, origin)
        )
        if tee is None:
            raise Exception("Revit did not insert a tee.")
        doc.Regenerate()
        main_arm = 0.0
        branch_arm = 0.0
        for connector in fitting_cable_tray_connectors(tee):
            offset = plan_vector(origin, connector.Origin)
            if abs(offset.DotProduct(across)) > abs(offset.DotProduct(along)):
                branch_arm = max(branch_arm, offset.GetLength())
            else:
                main_arm = max(main_arm, offset.GetLength())
        if main_arm <= EPSILON or branch_arm <= EPSILON:
            raise Exception("The inserted tee has unexpected connectors.")
        return {
            "main_arm": main_arm,
            "branch_arm": branch_arm,
            "name": fitting_type_name(tee.Symbol),
        }
    finally:
        if sub.GetStatus() == DB.TransactionStatus.Started:
            sub.RollBack()


def probe_bend(tray_type, level, origin, width, height, angle):
    """Build a throw-away S (horizontal -> offset -> horizontal), let Revit
    insert both vertical bends and measure the longest bend leg."""
    length = mm_to_internal(PROBE_LENGTH_MM)
    rise = mm_to_internal(PROBE_LENGTH_MM)
    along = DB.XYZ.BasisX
    run = 0.0 if angle >= 89.9 else rise / math.tan(math.radians(angle))
    bottom = origin
    top = origin + along.Multiply(run) + DB.XYZ.BasisZ.Multiply(rise)
    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        lower = create_tray(tray_type, level, bottom - along.Multiply(length),
                            bottom, width, height)
        slope = create_tray(tray_type, level, bottom, top, width, height)
        upper = create_tray(tray_type, level, top,
                            top + along.Multiply(length), width, height)
        doc.Regenerate()
        if run <= EPSILON:
            orient_vertical_tray(slope, along)
            doc.Regenerate()
        leg = 0.0
        for corner, first, second in ((bottom, lower, slope),
                                      (top, slope, upper)):
            bend = doc.Create.NewElbowFitting(
                nearest_free_connector(first, corner),
                nearest_free_connector(second, corner)
            )
            if bend is None:
                raise Exception("Revit did not insert a vertical bend.")
            doc.Regenerate()
            for connector in fitting_cable_tray_connectors(bend):
                leg = max(leg, connector.Origin.DistanceTo(corner))
        return leg
    finally:
        if sub.GetStatus() == DB.TransactionStatus.Started:
            sub.RollBack()


def probe_corner(tray_type, level, origin, width, height):
    """Build a throw-away 90 deg L, let Revit insert the tray type's
    Horizontal Bend and measure its leg (worst case for any corner)."""
    length = mm_to_internal(PROBE_LENGTH_MM)
    along = DB.XYZ.BasisX
    across = DB.XYZ.BasisY
    sub = DB.SubTransaction(doc)
    sub.Start()
    try:
        first = create_tray(tray_type, level, origin - along.Multiply(length),
                            origin, width, height)
        second = create_tray(tray_type, level, origin,
                             origin + across.Multiply(length), width, height)
        doc.Regenerate()
        bend = doc.Create.NewElbowFitting(
            nearest_free_connector(first, origin),
            nearest_free_connector(second, origin)
        )
        if bend is None:
            raise Exception("Revit did not insert a horizontal bend.")
        doc.Regenerate()
        leg = 0.0
        for connector in fitting_cable_tray_connectors(bend):
            leg = max(leg, plan_vector(origin, connector.Origin).GetLength())
        return leg
    finally:
        if sub.GetStatus() == DB.TransactionStatus.Started:
            sub.RollBack()


class FittingProbe(object):
    """Runs the probes in a transaction that is ALWAYS rolled back, and
    caches the measurements for the rest of the run."""

    def __init__(self, level, origin):
        self.level = level
        self.origin = origin
        self.cache = {}

    def _run(self, key, tray_type, assignments, action):
        if key in self.cache:
            return self.cache[key]
        transaction = DB.Transaction(doc, "Cable tray fitting check")
        transaction.Start()
        try:
            try:
                for role, symbol in assignments:
                    assign_fitting(tray_type, role, symbol)
                doc.Regenerate()
                result = (action(), None)
            except Exception as error:
                result = (None, str(error))
        finally:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        self.cache[key] = result
        return result

    def tee_arms(self, tray_type, tee_symbol, sizes):
        key = ("tee", element_id_value(tray_type.Id),
               element_id_value(tee_symbol.Id), tuple(sizes))

        def action():
            arms = []
            for width, height in sizes:
                try:
                    arms.append(probe_tee(tray_type, self.level, self.origin,
                                          width, height))
                except Exception as error:
                    raise Exception("{:g}x{:g} mm: {}".format(
                        width, height, error))
            return arms
        return self._run(key, tray_type, [("tee", tee_symbol)], action)

    def bend_legs(self, tray_type, bend_pair, sizes, angle):
        key = ("bend", element_id_value(tray_type.Id),
               tuple(element_id_value(item.Id) for item in bend_pair)
               if bend_pair else None,
               tuple(sizes), angle)
        assignments = []
        if bend_pair:
            assignments = [("inside_bend", bend_pair[0]),
                           ("outside_bend", bend_pair[1])]

        def action():
            legs = []
            for width, height in sizes:
                try:
                    legs.append(probe_bend(tray_type, self.level, self.origin,
                                           width, height, angle))
                except Exception as error:
                    raise Exception("{:g}x{:g} mm: {}".format(
                        width, height, error))
            return legs
        return self._run(key, tray_type, assignments, action)

    def corner_legs(self, tray_type, bend_symbol, sizes):
        key = ("corner", element_id_value(tray_type.Id),
               element_id_value(bend_symbol.Id) if bend_symbol else None,
               tuple(sizes))
        assignments = []
        if bend_symbol is not None:
            assignments = [("horizontal_bend", bend_symbol)]

        def action():
            legs = []
            for width, height in sizes:
                try:
                    legs.append(probe_corner(tray_type, self.level,
                                             self.origin, width, height))
                except Exception as error:
                    raise Exception("{:g}x{:g} mm: {}".format(
                        width, height, error))
            return legs
        return self._run(key, tray_type, assignments, action)


# =============================================================================
# TEE CROSSING PLANNER
# =============================================================================
def plan_tee_junctions(tee_nodes, routes, line_segments, lane_count,
                       run_spacing, tiers, crossing, fits):
    """For every tee junction, lane and tier decide where the tee sits
    (main lane x branch lane) and how far that lane is offset (rank x step).
    The main lane gets a local offset 'zone' around its tee; the branch lane
    gets an end 'cap' that rises (or drops) before the first main lane."""
    plans = []
    for junction_index, junction in enumerate(tee_nodes):
        main_start, main_end = line_segments[junction["main"]]
        branch_start, branch_end = line_segments[junction["branch"]]
        main_direction = horizontal_unit(main_start, main_end)
        main_normal = plan_normal(main_direction)
        branch_normal = plan_normal(horizontal_unit(branch_start, branch_end))

        # Which side of the main line does the branch come from?
        if junction["branch_endpoint"] == 0:
            far_end = branch_end
        else:
            far_end = branch_start
        towards_far_end = horizontal_unit(junction["point"], far_end)
        side = 1.0 if towards_far_end.DotProduct(main_normal) > 0 else -1.0

        # Rank 0 = main lane nearest the branch side.
        offsets = [
            lane_offset(index, lane_count, run_spacing)
            for index in range(lane_count)
        ]
        order = sorted(
            range(lane_count), key=lambda index: -side * offsets[index]
        )
        outer_lane = order[0]
        outer_start = main_start + main_normal.Multiply(offsets[outer_lane])
        outer_end = main_end + main_normal.Multiply(offsets[outer_lane])

        for lane in range(lane_count):
            rank = order.index(lane) if crossing["enabled"] else 0
            lift = lane_lift(rank, crossing["mode"], crossing["step"])
            main_lane_start = main_start + main_normal.Multiply(offsets[lane])
            main_lane_end = main_end + main_normal.Multiply(offsets[lane])
            branch_lane_start = branch_start + branch_normal.Multiply(
                offsets[lane]
            )
            branch_lane_end = branch_end + branch_normal.Multiply(
                offsets[lane]
            )
            centre_hit = line_intersection_2d(
                main_lane_start, main_lane_end,
                branch_lane_start, branch_lane_end
            )
            outer_hit = line_intersection_2d(
                outer_start, outer_end, branch_lane_start, branch_lane_end
            )
            if centre_hit is None or outer_hit is None:
                continue

            for tier_index in range(tiers):
                main_route = routes[(junction["main"], lane, tier_index)]
                branch_route = routes[(junction["branch"], lane, tier_index)]
                outer_route = routes[(junction["main"], outer_lane, tier_index)]
                base_z = main_route["start"].Z
                plan = {
                    "junction_index": junction_index,
                    "junction": junction,
                    "size_index": lane,
                    "tier_index": tier_index,
                    "rank": rank,
                    "lift": lift,
                    "center": DB.XYZ(
                        centre_hit[0].X, centre_hit[0].Y, base_z + lift
                    ),
                    "width": main_route["width"],
                    "height": main_route["height"],
                    "main_key": main_route["key"],
                    "branch_key": branch_route["key"],
                }
                plans.append(plan)
                main_route["zones"].append(plan)

                cap_name = (
                    "start_cap" if junction["branch_endpoint"] == 0
                    else "end_cap"
                )
                if branch_route[cap_name] is not None:
                    raise RouteGeometryError(
                        "Detail line {} ends on more than one main line at "
                        "the same point. Split the lines so each branch end "
                        "meets a single main line.".format(
                            junction["branch"] + 1
                        ),
                        junction["branch"]
                    )
                # The outer tee's branch arm / fillets reach towards the
                # branch side - crossing lanes must be offset before that.
                outer_reach = max(
                    mm_to_internal(outer_route["width"]) / 2.0,
                    fits[outer_lane]["branch_arm"]
                )
                branch_route[cap_name] = {
                    "plan": plan,
                    "lift": lift,
                    "outer_point": outer_hit[0],
                    "outer_reach": outer_reach,
                }
    return plans


def build_route_nodes(route, crossing):
    """Turn one lane into ordered (station, point, kind) nodes.
       kind: 'end' free end / L-corner, 'bend' vertical offset bend,
             'tee_main' main passes the tee centre (tray is split there),
             'tee_branch' branch ends at the tee centre."""
    start = route["start"]
    direction = route["direction"]
    length = plan_vector(start, route["end"]).GetLength()
    clearance = crossing["clearance"]
    run_factor = crossing["run_factor"]
    fit = route["fit"]
    up = DB.XYZ.BasisZ

    def station(point):
        return plan_vector(start, point).DotProduct(direction)

    def point_at(distance, lift):
        return start + direction.Multiply(distance) + up.Multiply(lift)

    nodes = []

    # --- Route start --------------------------------------------------------
    cap = route["start_cap"]
    if cap is None:
        nodes.append((0.0, start, LINK_NODE_KINDS[route["start_link"]]))
    else:
        centre = cap["plan"]["center"]
        nodes.append((station(centre), centre, "tee_branch"))
        if abs(cap["lift"]) > EPSILON:
            top = station(cap["outer_point"]) + cap["outer_reach"] + clearance
            bottom = top + abs(cap["lift"]) * run_factor
            nodes.append((top, point_at(top, cap["lift"]), "bend"))
            nodes.append((bottom, point_at(bottom, 0.0), "bend"))

    # --- Tees along this lane (it is the MAIN lane for them) -----------------
    zones = sorted(route["zones"], key=lambda item: station(item["center"]))
    for plan in zones:
        centre = plan["center"]
        centre_station = station(centre)
        lift = plan["lift"]
        if abs(lift) > EPSILON:
            run = abs(lift) * run_factor
            rise_top = centre_station - fit["main_arm"] - clearance
            fall_top = centre_station + fit["main_arm"] + clearance
            nodes.append(
                (rise_top - run, point_at(rise_top - run, 0.0), "bend")
            )
            nodes.append((rise_top, point_at(rise_top, lift), "bend"))
            nodes.append((centre_station, centre, "tee_main"))
            nodes.append((fall_top, point_at(fall_top, lift), "bend"))
            nodes.append(
                (fall_top + run, point_at(fall_top + run, 0.0), "bend")
            )
        else:
            nodes.append((centre_station, centre, "tee_main"))

    # --- Route end ------------------------------------------------------------
    cap = route["end_cap"]
    if cap is None:
        nodes.append(
            (length, route["end"], LINK_NODE_KINDS[route["end_link"]])
        )
    else:
        centre = cap["plan"]["center"]
        if abs(cap["lift"]) > EPSILON:
            top = station(cap["outer_point"]) - cap["outer_reach"] - clearance
            bottom = top - abs(cap["lift"]) * run_factor
            nodes.append((bottom, point_at(bottom, 0.0), "bend"))
            nodes.append((top, point_at(top, cap["lift"]), "bend"))
        nodes.append((station(centre), centre, "tee_branch"))

    problems = validate_route_nodes(route, nodes)
    if problems:
        error = problems[0]
        error.parts = problems      # every shortfall on this lane
        raise error
    return nodes


NODE_WORDS = {
    "end": "the line end",
    "corner": "the corner bend",
    "join": "the joint with the next line",
    "bend": "an offset bend",
    "tee_main": "the tee",
    "tee_branch": "the tee",
}

# Route end link (from adjusted_route_ends) -> node kind.
LINK_NODE_KINDS = {None: "end", "bend": "corner", "join": "join"}


def node_allowance(fit, kind):
    """Straight tray length a fitting at this node will use up."""
    if kind == "corner":
        return fit.get("corner_leg", 0.0)
    if kind == "bend":
        return fit["bend_leg"]
    if kind == "tee_main":
        return fit["main_arm"]
    if kind == "tee_branch":
        return fit["branch_arm"]
    return 0.0


def validate_route_nodes(route, nodes):
    """Make sure every fitting has room on the picked line, using the REAL
    fitting sizes measured by the probes. Returns ALL problems found."""
    problems = []
    fit = route["fit"]
    tolerance = mm_to_internal(1.0)
    min_straight = mm_to_internal(MIN_STRAIGHT_MM)
    label = "Line {} - {:g} x {:g} mm tray, tier {}".format(
        route["line_index"] + 1, route["width"], route["height"],
        route["tier_index"] + 1
    )
    for index in range(len(nodes) - 1):
        first_station, first_point, first_kind = nodes[index]
        second_station, second_point, second_kind = nodes[index + 1]

        # Sloped / vertical offset piece between two bends.
        if (first_kind == "bend" and second_kind == "bend"
                and abs(first_point.Z - second_point.Z) > tolerance):
            available = first_point.DistanceTo(second_point)
            needed = 2.0 * fit["bend_leg"] + min_straight
            if (second_station < first_station - tolerance
                    or available < needed - tolerance):
                problems.append(RouteGeometryError(
                    "{}: the offset slope is {:.0f} mm long but its two "
                    "bend fittings need {:.0f} mm.\n\nIncrease the offset "
                    "step or use a steeper bend angle.".format(
                        label, internal_to_mm(available),
                        internal_to_mm(needed)
                    ),
                    route["line_index"],
                    internal_to_mm(needed - available)
                ))
            continue

        # Straight piece: both end fittings + a little straight tray.
        needed = (node_allowance(fit, first_kind)
                  + node_allowance(fit, second_kind))
        needed = needed + min_straight if needed > EPSILON else tolerance
        available = second_station - first_station
        if available < needed - tolerance:
            shortfall = internal_to_mm(needed - available)
            side = None
            if first_kind == "end" and index == 0:
                side = "start"
            elif second_kind == "end" and index == len(nodes) - 2:
                side = "end"
            if "end" in (first_kind, second_kind):
                advice = (
                    "Extend detail line {} by at least {:.0f} mm, move the "
                    "junction further from the line end, or reduce the "
                    "offset step / clearance.".format(
                        route["line_index"] + 1, round_up_mm(shortfall)
                    )
                )
            elif "corner" in (first_kind, second_kind) or (
                    "join" in (first_kind, second_kind)):
                advice = (
                    "Move the tee junction at least {:.0f} mm further from "
                    "the corner, or reduce the offset step / clearance."
                    .format(round_up_mm(shortfall))
                )
            else:
                advice = (
                    "Move the junctions at least {:.0f} mm further apart, "
                    "or reduce the offset step / clearance.".format(
                        round_up_mm(shortfall)
                    )
                )
            problems.append(RouteGeometryError(
                "{}: the tray between {} and {} needs {:.0f} mm but only "
                "{:.0f} mm fits.\n\n{}".format(
                    label, NODE_WORDS[first_kind], NODE_WORDS[second_kind],
                    internal_to_mm(needed),
                    max(internal_to_mm(available), 0.0),
                    advice
                ),
                route["line_index"],
                shortfall,
                side
            ))
    return problems


def route_segments(route, nodes):
    """Straight tray pieces between consecutive nodes."""
    segments = []
    for index in range(len(nodes) - 1):
        _, first_point, first_kind = nodes[index]
        _, second_point, second_kind = nodes[index + 1]
        segments.append({
            "route_key": route["key"],
            "line_index": route["line_index"],
            "size_index": route["size_index"],
            "tier_index": route["tier_index"],
            "width": route["width"],
            "height": route["height"],
            "level": route["level"],
            "direction": route["direction"],
            "start": first_point,
            "end": second_point,
            "start_kind": first_kind,
            "end_kind": second_kind,
        })
    return segments


def build_all_segments(routes, route_order, crossing):
    """Build every lane; if any do not fit, report ALL shortfalls at once."""
    segments = []
    errors = []
    for route_key in route_order:
        route = routes[route_key]
        try:
            nodes = build_route_nodes(route, crossing)
        except RouteGeometryError as error:
            errors.append(error)
            continue
        segments.extend(route_segments(route, nodes))
    if errors:
        raise combine_route_errors(errors)
    return segments


def drawn_side(line_index, side):
    """'start' / 'end' as the user drew the line (lines may have been
    flipped internally to keep corners consistent)."""
    try:
        if line_flipped[line_index]:
            return "end" if side == "start" else "start"
    except (NameError, IndexError):
        pass
    return side


def combine_route_errors(errors):
    """Merge lane errors into one 'extend line X by ... mm' message."""
    extend = {}
    other = []
    flat = []
    for error in errors:
        flat.extend(error.parts or [error])
    for error in flat:
        if error.side in ("start", "end") and error.line_index is not None:
            sides = extend.setdefault(error.line_index, {})
            sides[error.side] = max(
                sides.get(error.side, 0.0), error.shortfall_mm
            )
        else:
            other.append(error)
    if not extend:
        return other[0]
    lines = [
        "The picked detail lines are too short for the tee fittings and "
        "the crossing offsets:"
    ]
    largest = 0.0
    for line_index in sorted(extend):
        parts = []
        for side in ("start", "end"):
            if side in extend[line_index]:
                amount = round_up_mm(extend[line_index][side])
                largest = max(largest, amount)
                parts.append("{:g} mm at its {} point".format(
                    amount, drawn_side(line_index, side)
                ))
        lines.append("  - Extend detail line {} by at least {}".format(
            line_index + 1, " and ".join(parts)
        ))
    if other:
        lines.append("  - " + str(other[0]).split("\n")[0])
    lines.append("")
    lines.append(
        "(Start point = where the detail line was first drawn.) Or move "
        "the junction further from the line ends, or reduce the offset "
        "step / clearance."
    )
    return RouteGeometryError(
        "\n".join(lines), sorted(extend)[0], largest
    )


def build_plan(candidate, tee_arms, bend_legs, corner_legs=None):
    """Full plan (lanes, tees, tray pieces) for a set of dialog values.
    Raises RouteGeometryError when it does not fit on the picked lines."""
    sizes = candidate["sizes"]
    lane_count = len(sizes)
    run_spacing = mm_to_internal(candidate["run_spacing"])
    tier_spacing = mm_to_internal(candidate["tier_spacing"])
    tiers = candidate["tiers"]
    # 1st tier height: reference level FFL + offset, measured to the
    # bottom / centre / top of the tray (each size gets its own centreline).
    reference_level = project_levels[candidate["level_index"]]
    first_tier_z = (level_elevation(reference_level)
                    + mm_to_internal(candidate["ffl_offset"]))
    crossing = {
        "enabled": candidate["avoid"],
        "mode": candidate["direction"],     # up / down / alternate
        "step": mm_to_internal(candidate["step"]),
        "clearance": mm_to_internal(candidate["clearance"]),
        # Horizontal run of an offset per unit of height (0 = 90 deg riser).
        "run_factor": (
            0.0 if candidate["angle"] >= 89.9
            else 1.0 / math.tan(math.radians(candidate["angle"]))
        ),
    }
    fits = []
    for index in range(lane_count):
        arms = tee_arms[index] or {}
        fits.append({
            "main_arm": arms.get("main_arm", 0.0),
            "branch_arm": arms.get("branch_arm", 0.0),
            "bend_leg": bend_legs[index] if bend_legs else 0.0,
            "corner_leg": corner_legs[index] if corner_legs else 0.0,
        })

    links = endpoint_links(line_segments)
    routes = {}
    route_order = []
    for line_index in range(len(line_segments)):
        for size_index, size in enumerate(sizes):
            start, end, start_link, end_link = adjusted_route_ends(
                line_index, line_segments, size_index, run_spacing,
                lane_count, links
            )
            lane_z = first_tier_z + mm_to_internal(
                reference_shift_mm(candidate["reference"], size[1])
            )
            for tier_index in range(tiers):
                tier_z = lane_z + tier_index * tier_spacing
                route_key = (line_index, size_index, tier_index)
                route_start = DB.XYZ(start.X, start.Y, tier_z)
                route_end = DB.XYZ(end.X, end.Y, tier_z)
                routes[route_key] = {
                    "key": route_key,
                    "line_index": line_index,
                    "size_index": size_index,
                    "tier_index": tier_index,
                    "start": route_start,
                    "end": route_end,
                    "start_link": start_link,
                    "end_link": end_link,
                    "direction": horizontal_unit(route_start, route_end),
                    "width": size[0],
                    "height": size[1],
                    "level": reference_level,
                    "fit": fits[size_index],
                    "zones": [],          # tees on this lane (it is the main)
                    "start_cap": None,    # this lane is a branch ending here
                    "end_cap": None,
                }
                route_order.append(route_key)

    tee_plans = plan_tee_junctions(
        tee_nodes, routes, line_segments, lane_count, run_spacing, tiers,
        crossing, fits
    )
    segments = build_all_segments(routes, route_order, crossing)
    return {
        "routes": routes,
        "route_order": route_order,
        "tee_plans": tee_plans,
        "segments": segments,
        "crossing": crossing,
        "fits": fits,
        "tee_arms": tee_arms,
        "bend_legs": bend_legs,
        "corner_legs": corner_legs,
    }


def try_build(candidate, tee_arms, bend_legs, corner_legs=None):
    try:
        return build_plan(candidate, tee_arms, bend_legs, corner_legs), None
    except RouteGeometryError as error:
        return None, error


# =============================================================================
# DIMENSION CHECK + SUGGESTIONS
# =============================================================================
def offset_requirements(legs, angle, tallest):
    """Minimum clearance / offset step for the measured vertical bends."""
    leg_mm = internal_to_mm(max(legs)) if legs else 0.0
    slope_mm = 2.0 * leg_mm + MIN_STRAIGHT_MM
    factor = 1.0 if angle >= 89.9 else math.sin(math.radians(angle))
    transition_step = slope_mm * factor
    return {
        "leg": leg_mm,
        "clearance": leg_mm + MIN_STRAIGHT_MM,
        "slope": slope_mm,
        "transition_step": transition_step,
        "step": max(tallest, transition_step),
    }


def apply_offset_minimums(proposed, reasons, legs, angle, tallest):
    need = offset_requirements(legs, angle, tallest)
    if proposed["clearance"] < need["clearance"] - 0.5:
        proposed["clearance"] = round_up_mm(need["clearance"])
        reasons["clearance"] = (
            "Each vertical bend fitting uses {:.0f} mm of straight tray; "
            "{:g} mm more is kept as plain tray.".format(
                need["leg"], MIN_STRAIGHT_MM
            )
        )
    if proposed["step"] < need["step"] - 0.5:
        if proposed["step"] < tallest:
            reasons["step"] = (
                "The offset step is below the tallest tray ({:g} mm), so "
                "crossing trays would clash.".format(tallest)
            )
        else:
            reasons["step"] = (
                "Two {:g} deg vertical bends need {:.0f} mm of sloped tray, "
                "which needs at least {:.0f} mm of offset.".format(
                    angle, need["slope"], need["transition_step"]
                )
            )
        proposed["step"] = round_up_mm(
            max(need["transition_step"], tallest + MIN_STRAIGHT_MM)
        )
    return need


def apply_tier_minimum(proposed, reasons, needs_offsets, tallest):
    if proposed["tiers"] <= 1:
        return
    lane_count = len(proposed["sizes"])
    required = tallest
    reason = "Tiers must be at least one tray height ({:g} mm) apart.".format(
        tallest
    )
    if needs_offsets:
        # One tier's rises and the next tier's drops must not meet.
        rise, drop = lift_extent(
            lane_count, proposed["direction"], proposed["step"]
        )
        required = rise + drop + tallest
        reason = (
            "Crossing lanes reach {} around each tee, so tiers must be at "
            "least {:g} mm apart.".format(describe_extent(rise, drop), required)
        )
    if proposed["tier_spacing"] < required - 0.5:
        proposed["tier_spacing"] = round_up_mm(required + MIN_STRAIGHT_MM)
        reasons["tier_spacing"] = reason


def apply_floor_minimum(proposed, reasons, tallest):
    """Crossing lanes that DROP must not end up below the level FFL (inside
    the slab) when the 1st tier itself is above the FFL."""
    rise, drop = lift_extent(
        len(proposed["sizes"]), proposed["direction"], proposed["step"]
    )
    if drop < 0.5:
        return
    lowest_bottom = min(
        proposed["ffl_offset"]
        + reference_shift_mm(proposed["reference"], height) - height / 2.0
        for _, height in proposed["sizes"]
    )
    if lowest_bottom < -0.5:
        return      # 1st tier is below FFL on purpose (e.g. floor void)
    dropped_bottom = lowest_bottom - drop
    if dropped_bottom >= -0.5:
        return
    proposed["ffl_offset"] = round_up_mm(
        proposed["ffl_offset"] - dropped_bottom
    )
    reasons["ffl_offset"] = (
        "Crossing lanes drop {:g} mm at the tees and would reach {:.0f} mm "
        "below the level FFL.".format(drop, -dropped_bottom)
    )


def short_reason(error):
    if error.line_index is None:
        return str(error).split("\n")[0]
    return (
        "Detail line {} is too short for the current offsets (about {:.0f} "
        "mm missing); the smallest workable values are used.".format(
            error.line_index + 1, error.shortfall_mm
        )
    )


def search_shorter_offsets(proposed, reasons, tray_type, tee_arms,
                           bend_legs, tallest, probe, first_error,
                           corner_legs=None):
    """The lines are too short for the current offsets: try the smallest
    workable clearance / step, then steeper bend angles."""
    sizes = proposed["sizes"]
    angles = [proposed["angle"]] + [
        value for value in (60.0, 90.0) if value > proposed["angle"] + 0.1
    ]
    # With 3+ lanes, alternating up/down halves the highest offset, which
    # also shortens the offset zones - try it after the user's direction.
    directions = [proposed["direction"]]
    if len(sizes) >= 3 and proposed["direction"] != "alternate":
        directions.append("alternate")
    best = None     # closest miss: (shortfall, candidate, error)
    for angle in angles:
        legs = bend_legs
        if abs(angle - proposed["angle"]) > 0.1:
            legs, error = probe.bend_legs(
                tray_type, proposed.get("bends"), sizes, angle
            )
            if error is not None:
                continue
        need = offset_requirements(legs, angle, tallest)
        # Keep the user's values where possible: user clearance / step
        # first, then the smallest workable ones.
        clearances = [proposed["clearance"], round_up_mm(need["clearance"])]
        steps = [proposed["step"], round_up_mm(need["step"])]
        for direction in directions:
            for clearance in clearances:
                if clearance < need["clearance"] - 0.5:
                    continue
                for step in steps:
                    if step < need["step"] - 0.5:
                        continue
                    if (direction == proposed["direction"]
                            and abs(angle - proposed["angle"]) < 0.1
                            and abs(clearance - proposed["clearance"]) < 0.5
                            and abs(step - proposed["step"]) < 0.5):
                        continue    # that is the layout that just failed
                    candidate = dict(proposed)
                    candidate.update({
                        "angle": angle, "clearance": clearance,
                        "step": step, "direction": direction,
                    })
                    candidate_reasons = {}
                    apply_tier_minimum(
                        candidate, candidate_reasons, True, tallest
                    )
                    plan, error = try_build(
                        candidate, tee_arms, legs, corner_legs
                    )
                    if plan is None:
                        if best is None or error.shortfall_mm < best[0]:
                            best = (error.shortfall_mm, candidate, error)
                        continue
                    why = short_reason(first_error)
                    for field in ("direction", "angle", "clearance", "step"):
                        if not same_value(field, candidate[field],
                                          proposed[field]):
                            reasons[field] = why
                    if candidate["direction"] == "alternate" and (
                            proposed["direction"] != "alternate"):
                        reasons["direction"] = (
                            "{} Alternating up / down halves the highest "
                            "offset, so the offset zones get shorter.".format(
                                why.split(";")[0] + "."
                            )
                        )
                    reasons.update(candidate_reasons)
                    proposed.update(candidate)
                    return plan, None
    if best is None:
        return None, first_error
    # Nothing fits: say how much longer the lines must be with the current
    # values AND with the smallest workable offsets.
    _, candidate, error = best
    extend_lines = [
        line for line in str(error).split("\n") if line.startswith("  - ")
    ] or [str(error).split("\n")[0]]
    message = (
        "{}\n\nWith the smallest workable offsets (offset step {:g} mm, "
        "clearance {:g} mm, {:g} deg bends, offsets {}) the lines still "
        "need:\n{}".format(
            str(first_error), candidate["step"], candidate["clearance"],
            candidate["angle"], DIRECTION_NAMES[candidate["direction"]],
            "\n".join(extend_lines)
        )
    )
    return None, RouteGeometryError(
        message, first_error.line_index, first_error.shortfall_mm
    )


FIELD_LABELS = (
    ("tee_index", "Tee fitting"),
    ("hbend", "Corner (horizontal) bend"),
    ("bends", "Vertical bend fittings"),
    ("ffl_offset", "Offset from FFL"),
    ("run_spacing", "Parallel spacing"),
    ("direction", "Offset direction"),
    ("step", "Offset step"),
    ("clearance", "Clearance"),
    ("angle", "Bend angle"),
    ("tier_spacing", "Tier spacing"),
)


def format_field(field, value):
    if field == "tee_index":
        if value is not None and 0 <= value < len(tee_options):
            return fitting_type_name(tee_options[value])
        return "(none)"
    if field == "bends":
        if not value:
            return "tray type default"
        return "{} / {}".format(
            fitting_type_name(value[0]), fitting_type_name(value[1])
        )
    if field == "hbend":
        return fitting_type_name(value) if value else "tray type default"
    if field == "angle":
        return "{:g} deg".format(value)
    if field == "direction":
        return DIRECTION_NAMES.get(value, value)
    return "{:g} mm".format(value)


def same_value(field, first, second):
    if field == "bends":
        def ids(pair):
            if not pair:
                return None
            return tuple(element_id_value(item.Id) for item in pair)
        return ids(first) == ids(second)
    if field == "hbend":
        def single_id(symbol):
            return element_id_value(symbol.Id) if symbol else None
        return single_id(first) == single_id(second)
    if isinstance(first, float) or isinstance(second, float):
        return abs(first - second) < 0.01
    return first == second


def evaluate(values, probe):
    """Check the entered values against the real fittings and the picked
    lines. Returns proposed values, the suggested changes, a fatal message
    (when nothing workable was found) and the finished plan."""
    proposed = dict(values)
    reasons = {}
    outcome = {"proposed": proposed, "changes": [], "fatal": None,
               "plan": None}
    sizes = values["sizes"]
    lane_count = len(sizes)
    tallest = max(size[1] for size in sizes)
    tray_type = tray_types[values["type_index"]]
    needs_tees = bool(tee_nodes)
    needs_offsets = needs_tees and values["avoid"] and lane_count > 1

    # 1. Parallel spacing - neighbouring tray widths must not overlap.
    widest_pair = 0.0
    for index in range(lane_count - 1):
        widest_pair = max(
            widest_pair, (sizes[index][0] + sizes[index + 1][0]) / 2.0
        )
    if values["run_spacing"] < widest_pair - 0.01:
        proposed["run_spacing"] = round_up_mm(widest_pair + MIN_STRAIGHT_MM)
        reasons["run_spacing"] = (
            "Neighbouring trays overlap; they need at least {:g} mm centre "
            "to centre ({:g} mm gap added).".format(
                widest_pair, MIN_STRAIGHT_MM
            )
        )

    # 2. Tee fitting - let Revit insert and size it on a throw-away T.
    tee_arms = [None] * lane_count
    if needs_tees:
        if not tee_options:
            outcome["fatal"] = (
                "T-junctions were found, but no cable tray tee family is "
                "loaded.\n\nLoad a tee family (for example 'M_Channel "
                "Horizontal Tee' or 'M_Ladder Horizontal Tee') and run the "
                "tool again."
            )
            return outcome
        current = values["tee_index"]
        if current is None or not 0 <= current < len(tee_options):
            current = 0
        order = [current] + [
            index for index in range(len(tee_options)) if index != current
        ]
        first_error = None
        for tee_index in order:
            arms, error = probe.tee_arms(
                tray_type, tee_options[tee_index], sizes
            )
            if error is None:
                tee_arms = arms
                proposed["tee_index"] = tee_index
                if tee_index != values["tee_index"]:
                    reasons["tee_index"] = (
                        "Revit could not insert the selected tee ({}).".format(
                            first_error or "not available"
                        )
                    )
                break
            if first_error is None:
                first_error = error
        else:
            outcome["fatal"] = (
                "Revit could not insert any loaded cable tray tee for these "
                "tray sizes.\n\nFirst error: {}\n\nCheck that the tee family "
                "suits the cable tray type (channel / ladder) and supports "
                "the tray sizes.".format(first_error)
            )
            return outcome

    # 2b. Corner (horizontal) bends where detail lines meet end to end.
    prefix = ""
    if tee_options:
        tee_index = proposed.get("tee_index")
        if tee_index is None or not 0 <= tee_index < len(tee_options):
            tee_index = 0
        prefix = family_prefix(tee_options[tee_index])
    corner_legs = [0.0] * lane_count
    if corner_count:
        legs, error = probe.corner_legs(
            tray_type, proposed.get("hbend"), sizes
        )
        if error is not None:
            for symbol in horizontal_bend_candidates(prefix):
                legs, symbol_error = probe.corner_legs(
                    tray_type, symbol, sizes
                )
                if symbol_error is None:
                    proposed["hbend"] = symbol
                    reasons["hbend"] = (
                        "Revit could not insert the tray type's Horizontal "
                        "Bend at the corners ({}).".format(error)
                    )
                    break
            else:
                outcome["fatal"] = (
                    "Corner bends could not be created where the detail "
                    "lines meet.\n\n{}\n\nAssign a 'Horizontal Bend' fitting "
                    "to the cable tray type (Type Properties > Fittings) or "
                    "load one (e.g. M_Channel Horizontal Bend).".format(error)
                )
                return outcome
        corner_legs = legs

    # 3. Vertical offset bends for the crossings.
    bend_legs = [0.0] * lane_count
    if needs_offsets:
        legs, error = probe.bend_legs(
            tray_type, proposed.get("bends"), sizes, values["angle"]
        )
        if error is not None:
            for pair in bend_pair_candidates(prefix):
                legs, pair_error = probe.bend_legs(
                    tray_type, pair, sizes, values["angle"]
                )
                if pair_error is None:
                    proposed["bends"] = pair
                    reasons["bends"] = (
                        "Revit could not insert the tray type's vertical "
                        "bends ({}).".format(error)
                    )
                    break
            else:
                outcome["fatal"] = (
                    "Vertical offset bends could not be created for the tee "
                    "crossings.\n\n{}\n\nAssign 'Vertical Inside Bend' and "
                    "'Vertical Outside Bend' fittings to the cable tray type "
                    "(Type Properties > Fittings), or switch off 'Offset "
                    "crossing lanes' in the dialog.".format(error)
                )
                return outcome
        bend_legs = legs
        apply_offset_minimums(
            proposed, reasons, legs, values["angle"], tallest
        )

    # 4. Tier spacing.
    apply_tier_minimum(proposed, reasons, needs_offsets, tallest)

    # 5. Does everything fit on the picked lines?
    plan, error = try_build(proposed, tee_arms, bend_legs, corner_legs)
    if error is not None and needs_offsets:
        plan, error = search_shorter_offsets(
            proposed, reasons, tray_type, tee_arms, bend_legs, tallest,
            probe, error, corner_legs
        )
    if error is None and needs_offsets:
        apply_floor_minimum(proposed, reasons, tallest)
        if not same_value("ffl_offset", values["ffl_offset"],
                          proposed["ffl_offset"]):
            plan, error = try_build(
                proposed, tee_arms, plan["bend_legs"], corner_legs
            )
    if error is not None:
        outcome["fatal"] = str(error)
        return outcome

    outcome["plan"] = plan
    for field, label in FIELD_LABELS:
        if not same_value(field, values.get(field), proposed.get(field)):
            outcome["changes"].append((
                label,
                format_field(field, values.get(field)),
                format_field(field, proposed.get(field)),
                reasons.get(field, "")
            ))
    return outcome


def format_changes(changes):
    lines = [
        "Some entered values will not work with the real fittings or the "
        "picked lines. Workable values:",
        "",
    ]
    for label, old, new, reason in changes:
        lines.append("{}:   {}   ->   {}".format(label, old, new))
        if reason:
            lines.append("      " + reason)
        lines.append("")
    lines.append("Apply these values and continue?")
    return "\n".join(lines)


# =============================================================================
# Input parsing / selection filter
# =============================================================================
def parse_sizes(text):
    sizes = []
    for entry in text.replace(";", ",").split(","):
        entry = entry.strip().lower().replace(" ", "")
        if not entry:
            continue
        dimensions = entry.split("x")
        if len(dimensions) != 2:
            raise ValueError(
                "Enter each size as width x height, for example "
                "300x100, 150x50."
            )
        try:
            width, height = float(dimensions[0]), float(dimensions[1])
        except ValueError:
            raise ValueError("Tray sizes must contain numeric width and height.")
        if (math.isnan(width) or math.isinf(width)
                or math.isnan(height) or math.isinf(height)
                or width <= 0 or height <= 0):
            raise ValueError("Tray widths and heights must be positive numbers.")
        sizes.append((width, height))
    if not sizes:
        raise ValueError("Enter at least one width x height tray size.")
    return sizes


class DetailLineFilter(ISelectionFilter):
    def AllowElement(self, element):
        if not isinstance(element, DB.DetailCurve):
            return False
        location = element.Location
        return (
            isinstance(location, DB.LocationCurve)
            and isinstance(location.Curve, DB.Line)
        )

    def AllowReference(self, reference, point):
        return False


class WarningSwallower(DB.IFailuresPreprocessor):
    """Removes Revit WARNINGS (not errors) raised while trays are created,
    e.g. 'elements overlap', so no warning dialog interrupts the run."""

    def __init__(self):
        self.count = 0

    def PreprocessFailures(self, failures_accessor):
        for message in list(failures_accessor.GetFailureMessages()):
            if message.GetSeverity() == DB.FailureSeverity.Warning:
                failures_accessor.DeleteWarning(message)
                self.count += 1
        return DB.FailureProcessingResult.Continue


# =============================================================================
# COMMENTS written on everything the tool creates
# =============================================================================
# The dialog's Comments card writes one text into the built-in "Comments"
# instance parameter (ALL_MODEL_INSTANCE_COMMENTS) of every tray - and of
# every fitting as well, if asked - so the new work can be filtered,
# scheduled and tagged straight away. The text may contain the tokens below;
# each one is replaced per element, so one template labels the whole route.
COMMENT_TOKENS = (
    ("{part}", "what the element is - Tray / Riser / Tee / Corner bend ..."),
    ("{type}", "cable tray type name"),
    ("{size}", "tray size, e.g. 300x100"),
    ("{width}", "tray width in mm"),
    ("{height}", "tray height in mm"),
    ("{lane}", "lane number (1 = first tray size in the list)"),
    ("{lanes}", "number of lanes"),
    ("{tier}", "tier number (1 = lowest)"),
    ("{tiers}", "number of tiers"),
    ("{line}", "detail line number, in the order they were picked"),
    ("{level}", "reference level name"),
    ("{offset}", "offset from the level FFL in mm"),
    ("{lift}", "crossing offset of this piece in mm (0 = on its tier)"),
    ("{tee}", "tee fitting type name"),
    ("{date}", "today's date (YYYY-MM-DD)"),
    ("{user}", "Revit user name"),
    ("{project}", "project file name"),
    ("{tool}", "name of this tool"),
)

# {part} - what each created element is called in the comment.
COMMENT_PART_NAMES = {
    "tray": "Tray",
    "offset": "Offset tray",
    "riser": "Riser",
    "tee": "Tee",
    "bend": "Offset bend",
    "corner": "Corner bend",
    "join": "Join",
}

# Which elements the comment is written on.
COMMENT_TARGET_NAMES = {
    "trays": "cable trays only",
    "all": "cable trays and fittings",
}

# What happens when the element already has a Comments value. Newly created
# elements are normally empty, but a family or a template can pre-fill it.
COMMENT_MODE_NAMES = {
    "replace": "replaced",
    "append": "added to",
}

COMMENT_SEPARATOR = u"; "    # between existing text and the new text
MAX_COMMENT_LENGTH = 255     # kept short enough for schedules and tags


def comment_token_hint():
    """One-line legend of the tokens, shown under the Comments box."""
    return u"Tokens: {} - replaced on each element.".format(
        u"  ".join(name for name, _ in COMMENT_TOKENS)
    )


def comment_token_tooltip():
    """Full token list for the Comments box tooltip."""
    return u"\n".join(
        u"{:<10}{}".format(name, description)
        for name, description in COMMENT_TOKENS
    )


def number_text(value):
    """'2700' from 2700.0, '2700' from '2700' - whatever the dialog holds."""
    try:
        return "{:g}".format(float(value))
    except (TypeError, ValueError):
        return "{}".format(value).strip()


def base_comment_tokens(values, tray_type, reference_level, tee_symbol=None):
    """Token values shared by every element of one run.

    The per-element tokens ({part}, {size}, {lane}, {tier}, {line}, {lift})
    are filled in here with the first lane of the first tier, so this set on
    its own is also the example shown in the dialog and in the preview."""
    sizes = values["sizes"]
    try:
        user_name = doc.Application.Username
    except Exception:
        user_name = ""
    try:
        project_name = doc.Title
    except Exception:
        project_name = ""
    return {
        "{part}": COMMENT_PART_NAMES["tray"],
        "{type}": tray_type_name(tray_type),
        "{size}": "{:g}x{:g}".format(sizes[0][0], sizes[0][1]),
        "{width}": number_text(sizes[0][0]),
        "{height}": number_text(sizes[0][1]),
        "{lane}": "1",
        "{lanes}": "{}".format(len(sizes)),
        "{tier}": "1",
        "{tiers}": number_text(values["tiers"]),
        "{line}": "1",
        "{level}": reference_level.Name,
        "{offset}": number_text(values["ffl_offset"]),
        "{lift}": "0",
        "{tee}": (
            fitting_type_name(tee_symbol) if tee_symbol is not None else ""
        ),
        "{date}": datetime.date.today().strftime("%Y-%m-%d"),
        "{user}": user_name,
        "{project}": project_name,
        "{tool}": TITLE,
    }


def element_comment_tokens(base, width, height, size_index, tier_index,
                           line_index, part, lift_mm=0.0):
    """Copy of the base tokens with this one element's values."""
    tokens = dict(base)
    tokens["{part}"] = COMMENT_PART_NAMES.get(part, part)
    tokens["{size}"] = "{:g}x{:g}".format(width, height)
    tokens["{width}"] = number_text(width)
    tokens["{height}"] = number_text(height)
    tokens["{lane}"] = "{}".format(size_index + 1)
    tokens["{tier}"] = "{}".format(tier_index + 1)
    tokens["{line}"] = "{}".format(line_index + 1)
    tokens["{lift}"] = (
        "{:+.0f}".format(lift_mm) if abs(lift_mm) >= 0.5 else "0"
    )
    return tokens


def format_comment(template, tokens):
    """Replace the tokens, tidy the spacing and keep it short enough."""
    text = template or u""
    for name, value in tokens.items():
        if name in text:
            text = text.replace(name, value)
    text = u" ".join(text.split())      # drop doubled / trailing spaces
    return text[:MAX_COMMENT_LENGTH]


def comment_parameter(element):
    """The writable 'Comments' instance parameter of an element, or None."""
    parameter = None
    try:
        parameter = element.get_Parameter(
            DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS
        )
    except Exception:
        parameter = None
    if parameter is None:
        try:
            parameter = element.LookupParameter("Comments")
        except Exception:
            parameter = None
    if parameter is None or parameter.IsReadOnly:
        return None
    return parameter


def write_comment(element, text, mode):
    """Write the Comments value. Returns True when it was written.

    'append' keeps whatever a family or template already put there and adds
    the new text after it (and never adds the same text twice)."""
    if element is None or not text:
        return False
    parameter = comment_parameter(element)
    if parameter is None:
        return False
    if mode == "append":
        try:
            existing = parameter.AsString()
        except Exception:
            existing = None
        if existing and existing.strip():
            if text in existing:
                return True
            text = (existing.strip() + COMMENT_SEPARATOR + text)
            text = text[:MAX_COMMENT_LENGTH]
    try:
        return bool(parameter.Set(text))
    except Exception:
        return False


def segment_comment_part(segment):
    """Which COMMENT_PART_NAMES entry a tray piece is."""
    if is_vertical_segment(segment):
        return "riser"
    if abs(segment["end"].Z - segment["start"].Z) > EPSILON:
        return "offset"
    return "tray"


def segment_lift_mm(routes, segment, point=None):
    """Height of a piece above its own tier in mm (0 = level with the tier)."""
    route = routes.get(segment["route_key"])
    if route is None:
        return 0.0
    if point is None:
        height = (segment["start"].Z + segment["end"].Z) / 2.0
    else:
        height = point.Z
    return internal_to_mm(height - route["start"].Z)


def fitting_note(element, part, width, height, size_index, tier_index,
                 line_index, lift_mm=0.0):
    """Record of one created fitting, kept only to write its Comments."""
    return {
        "element": element,
        "part": part,
        "width": width,
        "height": height,
        "size_index": size_index,
        "tier_index": tier_index,
        "line_index": line_index,
        "lift": lift_mm,
    }


# =============================================================================
# Remembered dialog settings (per pyRevit user config)
# =============================================================================
DEFAULT_SETTINGS = {
    "type_name": "",
    "tee_name": "",
    "sizes": "300x100, 150x50",
    "run_spacing": "400",
    "tiers": "1",
    "tier_spacing": "300",
    "level_name": "",           # empty = level of the active view
    "ffl_offset": "2700",       # 1st tier above the level FFL
    "reference": "bottom",      # bottom / centre / top of tray
    "avoid": "1",
    "direction": "up",
    "step": "250",
    "angle": "45",
    "clearance": "300",
    "comments_on": "1",
    "comments_text": "{tool} - {part} {size}, lane {lane}, tier {tier}, "
                     "line {line}, {level}",
    "comments_target": "trays",     # trays / all (trays and fittings)
    "comments_mode": "replace",     # replace / append to existing text
}


def load_settings():
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
        pass    # remembering settings is only a convenience


def values_to_settings(values):
    tee_index = values.get("tee_index")
    return {
        "type_name": tray_type_name(tray_types[values["type_index"]]),
        "tee_name": (
            fitting_type_name(tee_options[tee_index])
            if tee_index is not None and 0 <= tee_index < len(tee_options)
            else ""
        ),
        "sizes": ", ".join(
            "{:g}x{:g}".format(width, height)
            for width, height in values["sizes"]
        ),
        "run_spacing": "{:g}".format(values["run_spacing"]),
        "tiers": "{}".format(values["tiers"]),
        "tier_spacing": "{:g}".format(values["tier_spacing"]),
        "level_name": project_levels[values["level_index"]].Name,
        "ffl_offset": "{:g}".format(values["ffl_offset"]),
        "reference": values["reference"],
        "avoid": "1" if values["avoid"] else "0",
        "direction": values["direction"],
        "step": "{:g}".format(values["step"]),
        "angle": "{:g}".format(values["angle"]),
        "clearance": "{:g}".format(values["clearance"]),
        "comments_on": "1" if values.get("comments_on") else "0",
        "comments_text": values.get(
            "comments_text", DEFAULT_SETTINGS["comments_text"]
        ),
        "comments_target": values.get("comments_target", "trays"),
        "comments_mode": values.get("comments_mode", "replace"),
    }


# =============================================================================
# DIALOG (WPF)
# =============================================================================
FORM_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Cable Tray Auto Route Pro" Width="880" SizeToContent="Height"
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
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="#F1F5F9"/>
                <Setter Property="Foreground" Value="#94A3B8"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Height" Value="32"/>
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
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#1E40AF"/>
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
      <Setter Property="Padding" Value="20,0"/>
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
    <Style x:Key="ColumnHead" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
    </Style>
    <Style x:Key="LaneBadge" TargetType="Border">
      <Setter Property="Width" Value="22"/>
      <Setter Property="Height" Value="22"/>
      <Setter Property="CornerRadius" Value="11"/>
      <Setter Property="Background" Value="#F1F5F9"/>
      <Setter Property="HorizontalAlignment" Value="Left"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="LaneBadgeText" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#475569"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="TimesText" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#94A3B8"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="IconButton" TargetType="Button">
      <Setter Property="Width" Value="26"/>
      <Setter Property="Height" Value="26"/>
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="10"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="HorizontalAlignment" Value="Right"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="Transparent" CornerRadius="6">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#E2E8F0"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Opacity" Value="0.3"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="RemoveButton" TargetType="Button" BasedOn="{StaticResource IconButton}">
      <Setter Property="Foreground" Value="#DC2626"/>
      <Setter Property="FontSize" Value="11"/>
    </Style>
    <Style x:Key="AddButton" TargetType="Button">
      <Setter Property="Height" Value="34"/>
      <Setter Property="Foreground" Value="#2563EB"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid>
              <Rectangle x:Name="frame" RadiusX="6" RadiusY="6" Fill="#F8FBFF"
                         Stroke="#93C5FD" StrokeThickness="1.2"
                         StrokeDashArray="4 3"/>
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="frame" Property="Fill" Value="#EFF6FF"/>
                <Setter TargetName="frame" Property="Stroke" Value="#2563EB"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter Property="Opacity" Value="0.4"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="Segment" TargetType="RadioButton">
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RadioButton">
            <Border x:Name="bd" Background="White" BorderBrush="#CBD5E1"
                    BorderThickness="1" CornerRadius="8" Padding="8,8">
              <ContentPresenter HorizontalAlignment="Center"
                                VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="BorderBrush" Value="#93C5FD"/>
              </Trigger>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#EFF6FF"/>
                <Setter TargetName="bd" Property="BorderBrush" Value="#2563EB"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Opacity" Value="0.5"/>
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

    <!-- ===== Header ===== -->
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
                Data="M 2,6 L 2,18 L 28,18 L 28,6 M 8,18 L 8,12 M 15,18 L 15,12 M 22,18 L 22,12"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="Cable Tray Auto Route Pro" FontSize="20"
                     FontWeight="SemiBold" Foreground="White"/>
          <TextBlock Text="Multi-size, multi-tier cable trays from detail lines - with clash-free tee junctions"
                     FontSize="12" Foreground="#DBEAFE" Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <!-- ===== Body (scrolls on small screens; header/footer stay) ===== -->
    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto"
                  HorizontalScrollBarVisibility="Disabled">
    <Grid Margin="20,18,20,4">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="16"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>

      <!-- Left column -->
      <StackPanel Grid.Column="0">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,14">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="1" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Tray and fittings" Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <TextBlock Text="Cable tray type" Style="{StaticResource Label}"/>
            <ComboBox x:Name="cbType" Margin="0,0,0,12"/>
            <TextBlock Text="Tee fitting" Style="{StaticResource Label}"/>
            <ComboBox x:Name="cbTee"
                      ToolTip="Revit inserts this tee at every T-junction and sizes it to the trays automatically. It is set as the cable tray type's Tee fitting."/>
            <TextBlock Style="{StaticResource Hint}" Margin="0,5,0,12"
                       Text="Inserted and sized by Revit at every T-junction (set as the tray type's Tee fitting)."/>
            <DockPanel Margin="0,0,0,6">
              <TextBlock x:Name="tbLaneCount" DockPanel.Dock="Right"
                         Foreground="#64748B" FontSize="11"
                         VerticalAlignment="Bottom"/>
              <TextBlock Text="Tray sizes (one parallel lane each)"
                         Style="{StaticResource Label}" Margin="0"/>
            </DockPanel>
            <Grid Margin="0,0,0,4">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="30"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="20"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="84"/>
              </Grid.ColumnDefinitions>
              <TextBlock Grid.Column="0" Text="Lane" Style="{StaticResource ColumnHead}"/>
              <TextBlock Grid.Column="1" Text="Width (mm)" Style="{StaticResource ColumnHead}"/>
              <TextBlock Grid.Column="3" Text="Height (mm)" Style="{StaticResource ColumnHead}"/>
            </Grid>
            <ScrollViewer MaxHeight="216" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spSizes"/>
            </ScrollViewer>
            <Button x:Name="btnAddSize" Style="{StaticResource AddButton}"
                    Margin="0,2,0,0" ToolTip="Add another parallel cable tray lane">
              <StackPanel Orientation="Horizontal">
                <TextBlock Text="+" FontSize="17" FontWeight="Bold"
                           Margin="0,-3,8,0" VerticalAlignment="Center"/>
                <TextBlock Text="Add tray size" VerticalAlignment="Center"/>
              </StackPanel>
            </Button>
            <TextBlock Style="{StaticResource Hint}"
                       Text="Lanes are placed across the route in this order - use the arrows to reorder."/>
          </StackPanel>
        </Border>

        <!-- ===== Comments on everything created ===== -->
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,12">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="4" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Comments" Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <CheckBox x:Name="cbComments" Margin="0,0,0,12"
                      VerticalContentAlignment="Center"
                      ToolTip="Writes the text below into the Comments parameter of everything this tool creates. Nothing else in the model is touched.">
              <TextBlock Text="Write a Comments value on everything created"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>

            <StackPanel IsEnabled="{Binding IsChecked, ElementName=cbComments}">
              <TextBlock Text="Comments text" Style="{StaticResource Label}"/>
              <TextBox x:Name="tbComments"/>
              <TextBlock x:Name="tbCommentTokens" Style="{StaticResource Hint}"/>

              <Grid Margin="0,12,0,0">
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="10"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <StackPanel Grid.Column="0">
                  <TextBlock Text="Write it on" Style="{StaticResource Label}"/>
                  <ComboBox x:Name="cbCommentTarget"
                            ToolTip="Cable trays only, or the tees and bends as well. Fittings Revit adds by itself (not through this tool) are never touched.">
                    <ComboBoxItem Content="Cable trays only" Tag="trays"/>
                    <ComboBoxItem Content="Trays and fittings" Tag="all"/>
                  </ComboBox>
                </StackPanel>
                <StackPanel Grid.Column="2">
                  <TextBlock Text="If a value is already there" Style="{StaticResource Label}"/>
                  <ComboBox x:Name="cbCommentMode"
                            ToolTip="A new element is normally empty, but a family or template can pre-fill Comments. Replace overwrites it; Add keeps it and appends the new text.">
                    <ComboBoxItem Content="Replace it" Tag="replace"/>
                    <ComboBoxItem Content="Add to it" Tag="append"/>
                  </ComboBox>
                </StackPanel>
              </Grid>

              <Border Background="#FFFBEB" BorderBrush="#FDE68A"
                      BorderThickness="1" CornerRadius="6" Padding="10,8"
                      Margin="0,12,0,0">
                <TextBlock x:Name="tbCommentPreview" TextWrapping="Wrap"
                           Foreground="#92400E" FontSize="11.5"/>
              </Border>
            </StackPanel>
          </StackPanel>
        </Border>
      </StackPanel>

      <!-- Right column -->
      <StackPanel Grid.Column="2">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,12">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="2" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Tee junction clash avoidance"
                         Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <CheckBox x:Name="cbAvoid" Margin="0,0,0,12"
                      VerticalContentAlignment="Center">
              <TextBlock Text="Offset crossing lanes up / down at tee junctions"
                         FontWeight="SemiBold" Foreground="#334155"/>
            </CheckBox>

            <StackPanel IsEnabled="{Binding IsChecked, ElementName=cbAvoid}">
              <TextBlock Text="Offset direction" Style="{StaticResource Label}"/>
              <UniformGrid Columns="3" Margin="0,0,0,12">
                <RadioButton x:Name="rbUp" GroupName="CrossDirection"
                             Style="{StaticResource Segment}" Margin="0,0,4,0"
                             ToolTip="Crossing lanes are offset upwards: 0, +1, +2, +3 ... steps">
                  <StackPanel Orientation="Horizontal">
                    <Path Data="M 6,0 L 12,9 L 0,9 Z" Fill="#2563EB"
                          VerticalAlignment="Center" Margin="0,0,7,0"/>
                    <StackPanel>
                      <TextBlock Text="Rise over" FontWeight="SemiBold"/>
                      <TextBlock Text="lanes go up" FontSize="10.5"
                                 Foreground="#64748B"/>
                    </StackPanel>
                  </StackPanel>
                </RadioButton>
                <RadioButton x:Name="rbDown" GroupName="CrossDirection"
                             Style="{StaticResource Segment}" Margin="2,0,2,0"
                             ToolTip="Crossing lanes are offset downwards: 0, -1, -2, -3 ... steps">
                  <StackPanel Orientation="Horizontal">
                    <Path Data="M 0,0 L 12,0 L 6,9 Z" Fill="#2563EB"
                          VerticalAlignment="Center" Margin="0,0,7,0"/>
                    <StackPanel>
                      <TextBlock Text="Drop under" FontWeight="SemiBold"/>
                      <TextBlock Text="lanes go down" FontSize="10.5"
                                 Foreground="#64748B"/>
                    </StackPanel>
                  </StackPanel>
                </RadioButton>
                <RadioButton x:Name="rbAlt" GroupName="CrossDirection"
                             Style="{StaticResource Segment}" Margin="4,0,0,0"
                             ToolTip="Crossing lanes go up and down in turn: 0, +1, -1, +2, -2 ... steps. Halves the highest offset - recommended for 4 or more parallel trays.">
                  <StackPanel Orientation="Horizontal">
                    <Path Data="M 5,0 L 10,7 L 0,7 Z M 0,10 L 10,10 L 5,17 Z"
                          Fill="#2563EB" VerticalAlignment="Center"
                          Margin="0,0,7,0"/>
                    <StackPanel>
                      <TextBlock Text="Alternate" FontWeight="SemiBold"/>
                      <TextBlock Text="lanes up &amp; down" FontSize="10.5"
                                 Foreground="#64748B"/>
                    </StackPanel>
                  </StackPanel>
                </RadioButton>
              </UniformGrid>

              <Grid Margin="0,0,0,12">
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="10"/>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="10"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <StackPanel Grid.Column="0">
                  <TextBlock Text="Offset step (mm)" Style="{StaticResource Label}"/>
                  <TextBox x:Name="tbStep"
                           ToolTip="Vertical offset per crossing level, centre to centre. Lane 2 is offset 1 x step, lane 3 is offset 2 x step, and so on. A workable value is suggested if this is too small."/>
                </StackPanel>
                <StackPanel Grid.Column="2">
                  <TextBlock Text="Bend angle" Style="{StaticResource Label}"/>
                  <ComboBox x:Name="cbAngle"
                            ToolTip="Angle of the vertical offset bends. 45 degrees is the most reliable with standard bend families.">
                    <ComboBoxItem Content="30&#x00B0;" Tag="30"/>
                    <ComboBoxItem Content="45&#x00B0;" Tag="45"/>
                    <ComboBoxItem Content="60&#x00B0;" Tag="60"/>
                    <ComboBoxItem Content="90&#x00B0;" Tag="90"/>
                  </ComboBox>
                </StackPanel>
                <StackPanel Grid.Column="4">
                  <TextBlock Text="Clearance (mm)" Style="{StaticResource Label}"/>
                  <TextBox x:Name="tbClearance"
                           ToolTip="Straight tray kept between each offset bend and the tee or the tray being crossed. The real bend fittings are measured and a workable value is suggested if this is too small."/>
                </StackPanel>
              </Grid>

              <Border Background="#F8FAFC" BorderBrush="#E2E8F0"
                      BorderThickness="1" CornerRadius="8" Padding="10,10,10,8">
                <StackPanel>
                  <Canvas Width="330" Height="108" HorizontalAlignment="Center"
                          RenderTransformOrigin="0.5,0.5">
                    <Canvas.Style>
                      <Style TargetType="Canvas">
                        <Setter Property="RenderTransform">
                          <Setter.Value>
                            <ScaleTransform ScaleX="1" ScaleY="1"/>
                          </Setter.Value>
                        </Setter>
                        <Style.Triggers>
                          <DataTrigger Binding="{Binding IsChecked, ElementName=rbDown}" Value="True">
                            <Setter Property="RenderTransform">
                              <Setter.Value>
                                <ScaleTransform ScaleX="1" ScaleY="-1"/>
                              </Setter.Value>
                            </Setter>
                          </DataTrigger>
                          <DataTrigger Binding="{Binding IsChecked, ElementName=rbAlt}" Value="True">
                            <Setter Property="Visibility" Value="Collapsed"/>
                          </DataTrigger>
                        </Style.Triggers>
                      </Style>
                    </Canvas.Style>
                    <Line X1="6" Y1="84" X2="300" Y2="84" Stroke="#CBD5E1"
                          StrokeThickness="1" StrokeDashArray="4 3"/>
                    <Polyline Points="10,84 92,84 144,32 266,32" Stroke="#7C3AED"
                              StrokeThickness="6" StrokeLineJoin="Round"/>
                    <Polyline Points="10,84 118,84 144,58 222,58" Stroke="#2563EB"
                              StrokeThickness="6" StrokeLineJoin="Round"/>
                    <Polyline Points="10,84 176,84" Stroke="#0D9488"
                              StrokeThickness="6"/>
                    <Rectangle Canvas.Left="176" Canvas.Top="77" Width="28" Height="14"
                               Fill="#0D9488" RadiusX="2" RadiusY="2"/>
                    <Rectangle Canvas.Left="222" Canvas.Top="51" Width="28" Height="14"
                               Fill="#2563EB" RadiusX="2" RadiusY="2"/>
                    <Rectangle Canvas.Left="266" Canvas.Top="25" Width="28" Height="14"
                               Fill="#7C3AED" RadiusX="2" RadiusY="2"/>
                    <Line X1="314" Y1="84" X2="314" Y2="58" Stroke="#64748B" StrokeThickness="1.2"/>
                    <Line X1="314" Y1="58" X2="314" Y2="32" Stroke="#94A3B8"
                          StrokeThickness="1.2" StrokeDashArray="2 2"/>
                    <Line X1="308" Y1="84" X2="320" Y2="84" Stroke="#64748B" StrokeThickness="1.2"/>
                    <Line X1="308" Y1="58" X2="320" Y2="58" Stroke="#64748B" StrokeThickness="1.2"/>
                    <Line X1="308" Y1="32" X2="320" Y2="32" Stroke="#94A3B8" StrokeThickness="1.2"/>
                  </Canvas>
                  <!-- Alternate: 0, +1, -1, +2 steps -->
                  <Canvas Width="330" Height="96" HorizontalAlignment="Center">
                    <Canvas.Style>
                      <Style TargetType="Canvas">
                        <Setter Property="Visibility" Value="Collapsed"/>
                        <Style.Triggers>
                          <DataTrigger Binding="{Binding IsChecked, ElementName=rbAlt}" Value="True">
                            <Setter Property="Visibility" Value="Visible"/>
                          </DataTrigger>
                        </Style.Triggers>
                      </Style>
                    </Canvas.Style>
                    <Line X1="6" Y1="54" X2="300" Y2="54" Stroke="#CBD5E1"
                          StrokeThickness="1" StrokeDashArray="4 3"/>
                    <Polyline Points="10,54 78,54 120,12 288,12" Stroke="#7C3AED"
                              StrokeThickness="6" StrokeLineJoin="Round"/>
                    <Polyline Points="10,54 99,54 120,75 248,75" Stroke="#EA580C"
                              StrokeThickness="6" StrokeLineJoin="Round"/>
                    <Polyline Points="10,54 99,54 120,33 208,33" Stroke="#2563EB"
                              StrokeThickness="6" StrokeLineJoin="Round"/>
                    <Polyline Points="10,54 168,54" Stroke="#0D9488"
                              StrokeThickness="6"/>
                    <Rectangle Canvas.Left="168" Canvas.Top="48" Width="26" Height="12"
                               Fill="#0D9488" RadiusX="2" RadiusY="2"/>
                    <Rectangle Canvas.Left="208" Canvas.Top="27" Width="26" Height="12"
                               Fill="#2563EB" RadiusX="2" RadiusY="2"/>
                    <Rectangle Canvas.Left="248" Canvas.Top="69" Width="26" Height="12"
                               Fill="#EA580C" RadiusX="2" RadiusY="2"/>
                    <Rectangle Canvas.Left="288" Canvas.Top="6" Width="26" Height="12"
                               Fill="#7C3AED" RadiusX="2" RadiusY="2"/>
                  </Canvas>
                  <TextBlock x:Name="tbDiagramHint" Style="{StaticResource Hint}"/>
                </StackPanel>
              </Border>
            </StackPanel>

            <Border Background="#EFF6FF" CornerRadius="6" Padding="10,8"
                    Margin="0,10,0,0">
              <TextBlock x:Name="tbSummary" TextWrapping="Wrap"
                         Foreground="#1E3A8A" FontSize="11.5"/>
            </Border>
          </StackPanel>
        </Border>

        <Border Style="{StaticResource Card}">
          <StackPanel>
            <DockPanel Margin="0,0,0,14">
              <Border Style="{StaticResource Badge}">
                <TextBlock Text="3" Style="{StaticResource BadgeText}"/>
              </Border>
              <TextBlock Text="Levels and layout" Style="{StaticResource CardTitle}"/>
            </DockPanel>
            <!-- 1st tier height: reference level FFL + offset -->
            <Grid Margin="0,0,0,12">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="1.6*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="1.4*"/>
              </Grid.ColumnDefinitions>
              <StackPanel Grid.Column="0">
                <TextBlock Text="Reference level (FFL)" Style="{StaticResource Label}"/>
                <ComboBox x:Name="cbLevel"
                          ToolTip="Level whose Finished Floor Level the 1st tier is measured from. Defaults to the level of the current view."/>
              </StackPanel>
              <StackPanel Grid.Column="2">
                <TextBlock Text="Offset (mm)" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbFfl"
                         ToolTip="Height of the 1st tier above the level FFL (negative = below the floor, e.g. floor void)."/>
              </StackPanel>
              <StackPanel Grid.Column="4">
                <TextBlock Text="Measured to" Style="{StaticResource Label}"/>
                <ComboBox x:Name="cbReference"
                          ToolTip="Which part of the 1st-tier trays sits at FFL + offset. Bottom of tray keeps all sizes on one support level.">
                  <ComboBoxItem Content="Bottom of tray" Tag="bottom"/>
                  <ComboBoxItem Content="Centre of tray" Tag="centre"/>
                  <ComboBoxItem Content="Top of tray" Tag="top"/>
                </ComboBox>
              </StackPanel>
            </Grid>
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="10"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <StackPanel Grid.Column="0">
                <TextBlock Text="Lane spacing (mm)" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbRunSpacing"
                         ToolTip="Parallel spacing - centre to centre between lanes"/>
              </StackPanel>
              <StackPanel Grid.Column="2">
                <TextBlock Text="Tiers" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbTiers"
                         ToolTip="Vertical tiers - the 1st tier sits at FFL + offset, further tiers stack upwards"/>
              </StackPanel>
              <StackPanel Grid.Column="4">
                <TextBlock Text="Tier gap (mm)" Style="{StaticResource Label}"/>
                <TextBox x:Name="tbTierSpacing"
                         ToolTip="Tier spacing - from one tier to the next (same tray size)"/>
              </StackPanel>
            </Grid>
            <Border Background="#F0FDF4" CornerRadius="6" Padding="10,8"
                    Margin="0,12,0,0">
              <TextBlock x:Name="tbLevelSummary" TextWrapping="Wrap"
                         Foreground="#166534" FontSize="11.5"/>
            </Border>
          </StackPanel>
        </Border>
      </StackPanel>
    </Grid>
    </ScrollViewer>

    <!-- ===== Footer ===== -->
    <Border Grid.Row="2" Background="White" BorderBrush="#E2E8F0"
            BorderThickness="0,1,0,0" Padding="20,14">
      <DockPanel>
        <StackPanel DockPanel.Dock="Right" Orientation="Horizontal"
                    VerticalAlignment="Center">
          <Button x:Name="btnCancel" Content="Cancel"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,10,0"
                  IsCancel="True"/>
          <Button x:Name="btnCreate" Content="Select detail lines"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <TextBlock VerticalAlignment="Center" TextWrapping="Wrap"
                   Foreground="#64748B" FontSize="11" Margin="0,0,16,0"
                   Text="The real tee and bend fittings are measured first. If a dimension cannot work, workable values are suggested for your confirmation. A clash preview is shown before anything is created."/>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


MAX_TRAY_SIZES = 12

DIAGRAM_HINTS = {
    "up": (
        "Section along the branch: each lane further from the branch side "
        "rises one more step, so it passes over the main lanes in front of "
        "it. Its own main lane rises to the same level around the tee."
    ),
    "down": (
        "Section along the branch: each lane further from the branch side "
        "drops one more step, so it passes under the main lanes in front of "
        "it. Its own main lane drops to the same level around the tee."
    ),
    "alternate": (
        "Section along the branch: lanes go up and down in turn (0, +1, -1, "
        "+2, -2 steps ...). Every crossing lane stays at least one step "
        "clear of the lanes it passes, using about half the height of Rise "
        "over / Drop under."
    ),
}


def format_level(value):
    return "0" if abs(value) < 0.5 else "{:+.0f}".format(value)


class CableTrayForm(forms.WPFWindow):
    def __init__(self, tray_types, tee_options, settings, button_text):
        forms.WPFWindow.__init__(self, FORM_XAML, literal_string=True)
        self.tray_types = tray_types
        self.tee_options = tee_options
        self.values = None
        self.size_rows = []
        self.ready = False          # no live summary until fully built
        self.btnCreate.Content = button_text
        # Never taller than the screen: the middle scrolls, buttons stay.
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass

        # Tray types - reselect the one used last time if it still exists.
        selected = 0
        for index, tray_type in enumerate(tray_types):
            name = tray_type_name(tray_type)
            self.cbType.Items.Add(name)
            if name == settings.get("type_name"):
                selected = index
        if tray_types:
            self.cbType.SelectedIndex = selected

        # Tee fittings - last used, else the one on the tray type.
        if tee_options:
            for symbol in tee_options:
                self.cbTee.Items.Add(fitting_type_name(symbol))
            self.select_tee(settings.get("tee_name"))
        else:
            self.cbTee.Items.Add("(no cable tray tee family loaded)")
            self.cbTee.SelectedIndex = 0
            self.cbTee.IsEnabled = False

        # Reference levels - the level of the CURRENT VIEW is the default
        # (the remembered level only when the view has no level).
        current_index = -1
        remembered_index = -1
        for index, level_item in enumerate(project_levels):
            label = level_item.Name
            if (current_view_level is not None
                    and element_id_value(level_item.Id)
                    == element_id_value(current_view_level.Id)):
                label += "  (current)"
                current_index = index
            if level_item.Name == settings.get("level_name"):
                remembered_index = index
            self.cbLevel.Items.Add(label)
        if current_index >= 0:
            self.cbLevel.SelectedIndex = current_index
        elif remembered_index >= 0:
            self.cbLevel.SelectedIndex = remembered_index
        elif project_levels:
            self.cbLevel.SelectedIndex = 0
        self.tbFfl.Text = settings["ffl_offset"]
        for index in range(self.cbReference.Items.Count):
            if str(self.cbReference.Items[index].Tag) == settings["reference"]:
                self.cbReference.SelectedIndex = index
        if self.cbReference.SelectedIndex < 0:
            self.cbReference.SelectedIndex = 0

        # Restore the last-used values.
        self.tbRunSpacing.Text = settings["run_spacing"]
        self.tbTiers.Text = settings["tiers"]
        self.tbTierSpacing.Text = settings["tier_spacing"]
        self.tbStep.Text = settings["step"]
        self.tbClearance.Text = settings["clearance"]
        self.cbAvoid.IsChecked = settings["avoid"] == "1"
        if settings["direction"] == "down":
            self.rbDown.IsChecked = True
        elif settings["direction"] == "alternate":
            self.rbAlt.IsChecked = True
        else:
            self.rbUp.IsChecked = True
        self.select_angle(settings["angle"])

        # Comments written on everything created (tokens filled in per
        # element). Missing keys fall back to the defaults so an older
        # remembered config still opens.
        self.cbComments.IsChecked = settings.get("comments_on", "1") == "1"
        self.tbComments.Text = settings.get(
            "comments_text", DEFAULT_SETTINGS["comments_text"]
        )
        self.tbComments.ToolTip = (
            "Text written into the Comments parameter of every element "
            "created.\n\nTokens replaced on each element:\n\n"
            + comment_token_tooltip()
        )
        self.tbCommentTokens.Text = comment_token_hint()
        for combo, key in ((self.cbCommentTarget, "comments_target"),
                           (self.cbCommentMode, "comments_mode")):
            wanted = settings.get(key, DEFAULT_SETTINGS[key])
            for index in range(combo.Items.Count):
                if str(combo.Items[index].Tag) == wanted:
                    combo.SelectedIndex = index
                    break
            if combo.SelectedIndex < 0:
                combo.SelectedIndex = 0

        # Tray size rows (width / height boxes, + to add, arrows, remove).
        try:
            sizes = parse_sizes(settings["sizes"])
        except ValueError:
            sizes = parse_sizes(DEFAULT_SETTINGS["sizes"])
        self.set_sizes(sizes[:MAX_TRAY_SIZES])

        # Events (hooked after the initial values are set).
        self.cbType.SelectionChanged += self.on_type_changed
        for box in (self.tbStep, self.tbTiers, self.tbTierSpacing,
                    self.tbFfl, self.tbComments):
            box.TextChanged += self.on_inputs_changed
        self.cbLevel.SelectionChanged += self.on_inputs_changed
        self.cbReference.SelectionChanged += self.on_inputs_changed
        self.cbAvoid.Checked += self.on_inputs_changed
        self.cbAvoid.Unchecked += self.on_inputs_changed
        self.cbComments.Checked += self.on_inputs_changed
        self.cbComments.Unchecked += self.on_inputs_changed
        self.cbCommentTarget.SelectionChanged += self.on_inputs_changed
        self.cbCommentMode.SelectionChanged += self.on_inputs_changed
        self.rbUp.Checked += self.on_inputs_changed
        self.rbDown.Checked += self.on_inputs_changed
        self.rbAlt.Checked += self.on_inputs_changed
        self.btnAddSize.Click += self.on_add_size
        self.btnCreate.Click += self.on_create
        self.btnCancel.Click += self.on_cancel
        self.ready = True
        self.update_summary()

    # ----- tray size rows ------------------------------------------------------
    def set_sizes(self, sizes):
        self.size_rows = [
            self.make_size_row(width, height) for width, height in sizes
        ]
        self.layout_size_rows()

    def make_size_row(self, width, height):
        """One row: [lane no.] [width] x [height] [up] [down] [remove]."""
        row = Grid()
        row.Margin = Thickness(0, 0, 0, 6)
        for spec in (GridLength(30), GridLength(1, GridUnitType.Star),
                     GridLength(20), GridLength(1, GridUnitType.Star),
                     GridLength(28), GridLength(28), GridLength(28)):
            column = ColumnDefinition()
            column.Width = spec
            row.ColumnDefinitions.Add(column)

        badge = Border()
        badge.Style = self.FindResource("LaneBadge")
        badge_text = TextBlock()
        badge_text.Style = self.FindResource("LaneBadgeText")
        badge.Child = badge_text

        width_box = TextBox()
        width_box.Text = "{:g}".format(width)
        width_box.ToolTip = "Tray width in mm"
        times = TextBlock()
        times.Text = u"×"
        times.Style = self.FindResource("TimesText")
        height_box = TextBox()
        height_box.Text = "{:g}".format(height)
        height_box.ToolTip = "Tray height in mm"

        up_button = self.make_icon_button(
            u"▲", "Move this lane up", "IconButton")
        down_button = self.make_icon_button(
            u"▼", "Move this lane down", "IconButton")
        remove_button = self.make_icon_button(
            u"✕", "Remove this tray size", "RemoveButton")

        for column, element in enumerate((badge, width_box, times, height_box,
                                          up_button, down_button,
                                          remove_button)):
            Grid.SetColumn(element, column)
            row.Children.Add(element)

        entry = {
            "row": row, "badge_text": badge_text,
            "width": width_box, "height": height_box,
            "up": up_button, "down": down_button, "remove": remove_button,
        }
        up_button.Click += self.row_handler(self.move_size_row, entry, -1)
        down_button.Click += self.row_handler(self.move_size_row, entry, 1)
        remove_button.Click += self.row_handler(self.remove_size_row, entry, 0)
        width_box.TextChanged += self.on_inputs_changed
        height_box.TextChanged += self.on_inputs_changed
        return entry

    def make_icon_button(self, symbol, tooltip, style_name):
        button = Button()
        button.Content = symbol
        button.ToolTip = tooltip
        button.Style = self.FindResource(style_name)
        return button

    @staticmethod
    def row_handler(action, entry, argument):
        def handler(sender, args):
            action(entry, argument)
        return handler

    def row_index(self, entry):
        for index, item in enumerate(self.size_rows):
            if item is entry:
                return index
        return -1

    def layout_size_rows(self):
        self.spSizes.Children.Clear()
        count = len(self.size_rows)
        for index, entry in enumerate(self.size_rows):
            entry["badge_text"].Text = str(index + 1)
            entry["up"].IsEnabled = index > 0
            entry["down"].IsEnabled = index < count - 1
            entry["remove"].IsEnabled = count > 1
            self.spSizes.Children.Add(entry["row"])
        self.btnAddSize.IsEnabled = count < MAX_TRAY_SIZES
        self.tbLaneCount.Text = "{} lane{} in parallel".format(
            count, "" if count == 1 else "s"
        )
        self.update_summary()

    def on_add_size(self, sender, args):
        if len(self.size_rows) >= MAX_TRAY_SIZES:
            return
        width, height = 150.0, 50.0
        if self.size_rows:     # start from the last size - easy to tweak
            try:
                width = float(self.size_rows[-1]["width"].Text.strip())
                height = float(self.size_rows[-1]["height"].Text.strip())
            except ValueError:
                pass
        entry = self.make_size_row(width, height)
        self.size_rows.append(entry)
        self.layout_size_rows()
        entry["width"].Focus()
        entry["width"].SelectAll()

    def move_size_row(self, entry, delta):
        index = self.row_index(entry)
        target = index + delta
        if index < 0 or not 0 <= target < len(self.size_rows):
            return
        self.size_rows[index], self.size_rows[target] = (
            self.size_rows[target], self.size_rows[index]
        )
        self.layout_size_rows()

    def remove_size_row(self, entry, unused):
        index = self.row_index(entry)
        if index < 0 or len(self.size_rows) <= 1:
            return
        del self.size_rows[index]
        self.layout_size_rows()

    def read_sizes(self):
        sizes = []
        for index, entry in enumerate(self.size_rows):
            sizes.append((
                self.read_size_value(entry["width"].Text, index, "width"),
                self.read_size_value(entry["height"].Text, index, "height"),
            ))
        if not sizes:
            raise ValueError("Add at least one tray size.")
        return sizes

    @staticmethod
    def read_size_value(text, index, label):
        try:
            value = float(text.strip())
        except ValueError:
            raise ValueError(
                "Lane {}: enter the tray {} in mm.".format(index + 1, label)
            )
        if math.isnan(value) or math.isinf(value) or value <= 0:
            raise ValueError(
                "Lane {}: the tray {} must be greater than zero.".format(
                    index + 1, label
                )
            )
        return value

    def current_direction(self):
        if self.rbAlt.IsChecked == True:
            return "alternate"
        if self.rbDown.IsChecked == True:
            return "down"
        return "up"

    def select_tee(self, preferred_name=None):
        names = [fitting_type_name(symbol) for symbol in self.tee_options]
        if preferred_name and preferred_name in names:
            self.cbTee.SelectedIndex = names.index(preferred_name)
            return
        if self.cbType.SelectedIndex >= 0:
            assigned = assigned_fitting(
                self.tray_types[self.cbType.SelectedIndex], "tee"
            )
            if assigned is not None:
                for index, symbol in enumerate(self.tee_options):
                    if element_id_value(symbol.Id) == element_id_value(assigned.Id):
                        self.cbTee.SelectedIndex = index
                        return
        self.cbTee.SelectedIndex = 0

    def select_angle(self, angle_text):
        for index in range(self.cbAngle.Items.Count):
            if str(self.cbAngle.Items[index].Tag) == str(angle_text):
                self.cbAngle.SelectedIndex = index
                return
        self.cbAngle.SelectedIndex = 1     # 45 degrees

    @staticmethod
    def read_number(text, label):
        try:
            value = float(text.strip())
        except ValueError:
            raise ValueError("{} must be a number.".format(label))
        if math.isnan(value) or math.isinf(value) or value <= 0:
            raise ValueError("{} must be greater than zero.".format(label))
        return value

    @staticmethod
    def read_offset(text):
        try:
            value = float(text.strip())
        except ValueError:
            raise ValueError("Offset from FFL must be a number (mm).")
        if math.isnan(value) or math.isinf(value):
            raise ValueError("Offset from FFL must be finite.")
        return value

    def current_reference(self):
        item = self.cbReference.SelectedItem
        return str(item.Tag) if item is not None else "bottom"

    def current_comment_target(self):
        item = self.cbCommentTarget.SelectedItem
        return str(item.Tag) if item is not None else "trays"

    def current_comment_mode(self):
        item = self.cbCommentMode.SelectedItem
        return str(item.Tag) if item is not None else "replace"

    def update_comment_preview(self):
        """Show the Comments text as the first tray will read it."""
        if self.cbComments.IsChecked != True:
            self.tbCommentPreview.Text = (
                "No Comments value is written - any existing text is left "
                "alone."
            )
            return
        template = self.tbComments.Text or ""
        if not template.strip():
            self.tbCommentPreview.Text = (
                "Enter the Comments text, or switch this option off."
            )
            return
        tray_type = (
            self.tray_types[self.cbType.SelectedIndex]
            if 0 <= self.cbType.SelectedIndex < len(self.tray_types) else None
        )
        level = (
            project_levels[self.cbLevel.SelectedIndex]
            if 0 <= self.cbLevel.SelectedIndex < len(project_levels) else None
        )
        if tray_type is None or level is None:
            self.tbCommentPreview.Text = (
                "Select the cable tray type and the reference level to "
                "preview the Comments text."
            )
            return
        try:
            sizes = self.read_sizes()
        except ValueError:
            self.tbCommentPreview.Text = (
                "Enter valid tray sizes to preview the Comments text."
            )
            return
        tee_symbol = None
        if self.tee_options and self.cbTee.SelectedIndex >= 0:
            tee_symbol = self.tee_options[self.cbTee.SelectedIndex]
        tokens = base_comment_tokens(
            {
                "sizes": sizes,
                "tiers": self.tbTiers.Text,
                "ffl_offset": self.tbFfl.Text,
            },
            tray_type, level, tee_symbol
        )
        text = format_comment(template, tokens)
        note = "Written on {}".format(
            COMMENT_TARGET_NAMES[self.current_comment_target()]
        )
        if self.current_comment_mode() == "append":
            note += "; any existing text is kept and this is added after it"
        unknown = [
            name for name in set(
                part.split("}")[0] + "}"
                for part in template.split("{")[1:] if "}" in part
            )
            if name not in tokens
        ]
        if unknown:
            note += ". Not a token (left as typed): {}".format(
                ", ".join(sorted(unknown))
            )
        self.tbCommentPreview.Text = u'First tray reads "{}".\n{}.'.format(
            text, note
        )

    def update_level_summary(self):
        """Absolute heights of the 1st tier (and further tiers)."""
        if self.cbLevel.SelectedIndex < 0 or not project_levels:
            self.tbLevelSummary.Text = "Select the reference level."
            return
        level_item = project_levels[self.cbLevel.SelectedIndex]
        try:
            offset = float(self.tbFfl.Text.strip())
        except ValueError:
            self.tbLevelSummary.Text = "Enter the offset from FFL in mm."
            return
        reference = self.current_reference()
        ffl = internal_to_mm(level_item.Elevation)
        text = (
            "1st tier: {} at {} (FFL {:+.0f}) {:+.0f} mm  =  {:+.0f} mm."
            .format(REFERENCE_NAMES[reference], level_item.Name, ffl,
                    offset, ffl + offset)
        )
        try:
            tiers = int(self.tbTiers.Text.strip())
            gap = float(self.tbTierSpacing.Text.strip())
            if tiers > 1:
                text += " Tiers 2-{} every {:.0f} mm above (up to {:+.0f} mm).".format(
                    tiers, gap, ffl + offset + gap * (tiers - 1)
                )
        except ValueError:
            pass
        try:
            sizes = self.read_sizes()
            step = float(self.tbStep.Text.strip())
            if self.cbAvoid.IsChecked == True and len(sizes) > 1:
                rise, drop = lift_extent(
                    len(sizes), self.current_direction(), step
                )
                lowest = min(
                    offset + reference_shift_mm(reference, height)
                    - height / 2.0 for _, height in sizes
                ) - drop
                if lowest < 0 <= lowest + drop:
                    text += (
                        " Note: dropped crossing lanes would go {:.0f} mm "
                        "below FFL - a higher offset will be suggested."
                        .format(-lowest)
                    )
        except Exception:
            pass
        self.tbLevelSummary.Text = text

    def on_type_changed(self, sender, args):
        if self.tee_options:
            self.select_tee(None)

    def on_inputs_changed(self, sender, args):
        self.update_summary()

    def update_summary(self):
        """Show the lane levels the tee crossings will use."""
        if not self.ready:
            return
        self.update_level_summary()
        self.update_comment_preview()
        mode = self.current_direction()
        self.tbDiagramHint.Text = DIAGRAM_HINTS[mode]
        try:
            sizes = self.read_sizes()
            step = float(self.tbStep.Text.strip())
        except Exception:
            self.tbSummary.Text = (
                "Enter valid tray sizes and an offset step to preview the "
                "crossing levels."
            )
            return
        if self.cbAvoid.IsChecked != True:
            self.tbSummary.Text = (
                "Crossing offsets are OFF - branch lanes join their main "
                "lanes at one level and can clash with the lanes in front."
            )
            return
        if len(sizes) < 2:
            self.tbSummary.Text = (
                "Only one tray size - tees need no crossing offset."
            )
            return
        levels_text = [
            format_level(lane_lift(rank, mode, step))
            for rank in range(len(sizes))
        ]
        tallest = max(size[1] for size in sizes)
        text = "Lane levels at each tee: {} mm.".format(
            " / ".join(levels_text)
        )
        if step < tallest:
            text += (
                " Offset step is below the tallest tray ({:.0f} mm) - a "
                "workable value will be suggested.".format(tallest)
            )
        else:
            text += " Clear gap between crossing trays: {:.0f} mm.".format(
                step - tallest
            )
        if mode != "alternate" and len(sizes) >= 4:
            rise, drop = lift_extent(len(sizes), "alternate", step)
            used = lift_extent(len(sizes), mode, step)
            text += (
                " Tip: with {} trays in parallel, 'Alternate' keeps offsets "
                "within +{:.0f} / -{:.0f} mm instead of {:.0f} mm.".format(
                    len(sizes), rise, drop, max(used)
                )
            )
        self.tbSummary.Text = text

    def on_create(self, sender, args):
        try:
            if self.cbType.SelectedIndex < 0:
                raise ValueError("Select a cable tray type.")
            sizes = self.read_sizes()
            run_spacing = self.read_number(
                self.tbRunSpacing.Text, "Parallel spacing"
            )
            tier_spacing = self.read_number(
                self.tbTierSpacing.Text, "Tier spacing"
            )
            if self.cbLevel.SelectedIndex < 0:
                raise ValueError("Select the reference level (FFL).")
            ffl_offset = self.read_offset(self.tbFfl.Text)
            try:
                tiers = int(self.tbTiers.Text.strip())
            except ValueError:
                raise ValueError("Vertical tiers must be a whole number.")
            if tiers < 1:
                raise ValueError("Vertical tiers must be at least one.")
            step = self.read_number(self.tbStep.Text, "Offset step")
            clearance = self.read_number(self.tbClearance.Text, "Clearance")
            angle = float(str(self.cbAngle.SelectedItem.Tag))
            comments_on = self.cbComments.IsChecked == True
            comments_text = (self.tbComments.Text or "").strip()
            if comments_on and not comments_text:
                raise ValueError(
                    "Enter the Comments text, or switch the Comments option "
                    "off."
                )
            self.values = {
                "type_index": self.cbType.SelectedIndex,
                "tee_index": (
                    self.cbTee.SelectedIndex if self.tee_options else -1
                ),
                "sizes": sizes,
                "run_spacing": run_spacing,
                "tiers": tiers,
                "tier_spacing": tier_spacing,
                "level_index": self.cbLevel.SelectedIndex,
                "ffl_offset": ffl_offset,
                "reference": self.current_reference(),
                "avoid": self.cbAvoid.IsChecked == True,
                "direction": self.current_direction(),
                "step": step,
                "angle": angle,
                "clearance": clearance,
                "comments_on": comments_on,
                "comments_text": comments_text,
                "comments_target": self.current_comment_target(),
                "comments_mode": self.current_comment_mode(),
                "bends": None,
                "hbend": None,
            }
            self.Close()
        except ValueError as error:
            forms.alert(str(error), title=TITLE, warn_icon=True)

    def on_cancel(self, sender, args):
        self.values = None
        self.Close()


def run_form(settings, button_text, previous=None):
    """Show the dialog; exits the script on Cancel."""
    form = CableTrayForm(tray_types, tee_options, settings, button_text)
    form.ShowDialog()
    if form.values is None:
        raise SystemExit
    new_values = form.values
    # Fitting families accepted earlier stay accepted for the same tray type.
    if (previous is not None
            and previous["type_index"] == new_values["type_index"]):
        for key in ("bends", "hbend"):
            if previous.get(key):
                new_values[key] = previous[key]
    save_settings(user_config, values_to_settings(new_values))
    return new_values


# =============================================================================
# CLASH PREVIEW (tabbed, read-only - shown before anything is created)
# =============================================================================
# Three tabs: what will be created, every clash with the services already in
# the model (linked models included on request) and the planned trays that
# cross each other. Each service row can be selected or zoomed to in Revit,
# and the check can be re-run with a different clearance without leaving the
# window. The clearance / linked-model choice is kept for the rest of the
# session, so going back to the settings and returning does not reset it.
PREVIEW_STATE = {
    "clearance": DEFAULT_SERVICE_CLEARANCE_MM,
    "links": False,
}

MAX_PREVIEW_ROWS = 200      # keeps the window responsive on busy models

CLASH_XAML = """
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Cable Tray Clash Preview" Width="960" SizeToContent="Height"
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
    <Style x:Key="Label" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#334155"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Margin" Value="0,0,0,5"/>
    </Style>
    <Style x:Key="Hint" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Height" Value="30"/>
      <Setter Property="Padding" Value="8,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="BorderBrush" Value="#CBD5E1"/>
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
    <Style x:Key="SmallButton" TargetType="Button"
           BasedOn="{StaticResource SecondaryButton}">
      <Setter Property="Height" Value="26"/>
      <Setter Property="Padding" Value="10,0"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="FontWeight" Value="Normal"/>
      <Setter Property="Margin" Value="4,0,0,0"/>
    </Style>
    <Style x:Key="RowCardBad" TargetType="Border">
      <Setter Property="Background" Value="#FEF2F2"/>
      <Setter Property="BorderBrush" Value="#FCA5A5"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="8"/>
      <Setter Property="Padding" Value="10,8"/>
      <Setter Property="Margin" Value="0,0,0,6"/>
    </Style>
    <Style x:Key="RowCardGood" TargetType="Border">
      <Setter Property="Background" Value="#F0FDF4"/>
      <Setter Property="BorderBrush" Value="#BBF7D0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="8"/>
      <Setter Property="Padding" Value="10,8"/>
      <Setter Property="Margin" Value="0,0,0,6"/>
    </Style>
    <Style x:Key="RowTitle" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#0F172A"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="TextTrimming" Value="CharacterEllipsis"/>
    </Style>
    <Style x:Key="RowMeta" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#64748B"/>
      <Setter Property="FontSize" Value="11"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="Margin" Value="0,2,0,0"/>
    </Style>
    <Style x:Key="RowBad" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#B91C1C"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="FontSize" Value="11.5"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="RowGood" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#166534"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="StatusGood" TargetType="Border">
      <Setter Property="Background" Value="#F0FDF4"/>
      <Setter Property="BorderBrush" Value="#BBF7D0"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="6"/>
      <Setter Property="Padding" Value="10,7"/>
    </Style>
    <Style x:Key="StatusBad" TargetType="Border">
      <Setter Property="Background" Value="#FEF2F2"/>
      <Setter Property="BorderBrush" Value="#FCA5A5"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="6"/>
      <Setter Property="Padding" Value="10,7"/>
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
                Data="M 2,14 L 24,14 M 13,3 L 13,25 M 7,8 L 19,20 M 19,8 L 7,20"/>
        </Border>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="Clash preview" FontSize="19" FontWeight="SemiBold"
                     Foreground="White"/>
          <TextBlock x:Name="tbHeadline" FontSize="12" Foreground="#DBEAFE"
                     Margin="0,2,0,0"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <!-- ===== Tabs ===== -->
    <TabControl Grid.Row="1" Margin="20,14,20,0" BorderThickness="0">

      <!-- Overview -->
      <TabItem x:Name="tabOverview">
        <Border Style="{StaticResource Card}">
          <ScrollViewer MaxHeight="430" VerticalScrollBarVisibility="Auto">
            <TextBlock x:Name="tbOverview" TextWrapping="Wrap"
                       FontFamily="Consolas" FontSize="11.5"
                       Foreground="#1E293B"/>
          </ScrollViewer>
        </Border>
      </TabItem>

      <!-- Existing services -->
      <TabItem x:Name="tabServices">
        <Border Style="{StaticResource Card}">
          <Grid>
            <Grid.RowDefinitions>
              <RowDefinition Height="Auto"/>
              <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <Grid Grid.Row="0" Margin="0,0,0,10">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
              </Grid.ColumnDefinitions>
              <TextBlock x:Name="tbServiceStatus" Grid.Column="0"
                         Style="{StaticResource Hint}"
                         VerticalAlignment="Center" Margin="0,0,14,0"/>
              <StackPanel Grid.Column="1" Orientation="Horizontal"
                          VerticalAlignment="Center" Margin="0,0,12,0">
                <TextBlock Text="Clearance (mm)" VerticalAlignment="Center"
                           Foreground="#334155" FontWeight="SemiBold"
                           Margin="0,0,8,0"/>
                <TextBox x:Name="tbServiceClearance" Width="64"
                         ToolTip="The planned trays are grown by this much before the check, so a near miss shows up as a clash. 0 reports only real overlaps."/>
              </StackPanel>
              <CheckBox x:Name="cbIncludeLinks" Grid.Column="2"
                        VerticalAlignment="Center" Margin="0,0,12,0"
                        VerticalContentAlignment="Center"
                        ToolTip="Also check the services in loaded linked models (other disciplines usually live there). Their elements cannot be selected from this model.">
                <TextBlock Text="Linked models" Foreground="#334155"
                           FontWeight="SemiBold"/>
              </CheckBox>
              <Button x:Name="btnRecheck" Grid.Column="3" Content="Re-check"
                      Style="{StaticResource SecondaryButton}" Height="30"/>
            </Grid>
            <ScrollViewer Grid.Row="1" MaxHeight="370"
                          VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spServices"/>
            </ScrollViewer>
          </Grid>
        </Border>
      </TabItem>

      <!-- Tray-to-tray -->
      <TabItem x:Name="tabInternal">
        <Border Style="{StaticResource Card}">
          <StackPanel>
            <TextBlock Style="{StaticResource Hint}" Margin="0,0,0,10"
                       Text="Planned trays whose centrelines cross in plan without enough vertical separation. Raise the offset step, or switch the crossing direction to Alternate, to clear them."/>
            <ScrollViewer MaxHeight="390" VerticalScrollBarVisibility="Auto">
              <StackPanel x:Name="spInternal"/>
            </ScrollViewer>
          </StackPanel>
        </Border>
      </TabItem>
    </TabControl>

    <!-- ===== Footer ===== -->
    <Border Grid.Row="2" Background="White" BorderBrush="#E2E8F0"
            BorderThickness="0,1,0,0" Padding="20,14" Margin="0,14,0,0">
      <DockPanel>
        <StackPanel DockPanel.Dock="Right" Orientation="Horizontal"
                    VerticalAlignment="Center">
          <Button x:Name="btnCopy" Content="Copy report"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnEdit" Content="Back to settings"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,8,0"/>
          <Button x:Name="btnCancel" Content="Cancel"
                  Style="{StaticResource SecondaryButton}" Margin="0,0,10,0"
                  IsCancel="True"/>
          <Button x:Name="btnCreate" Content="Create the cable trays"
                  Style="{StaticResource PrimaryButton}" IsDefault="True"/>
        </StackPanel>
        <Border x:Name="brdStatus" Style="{StaticResource StatusGood}"
                VerticalAlignment="Center" Margin="0,0,16,0">
          <TextBlock x:Name="tbStatus" TextWrapping="Wrap" FontSize="11.5"/>
        </Border>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


def preview_lines(plan, values, internal, clashes, clearance_mm,
                  include_links):
    """The Overview tab - what will be created, and the headline checks."""
    sizes = values["sizes"]
    segments = plan["segments"]
    tee_plans = plan["tee_plans"]
    crossing = plan["crossing"]
    tray_type = tray_types[values["type_index"]]
    tee_symbol = None
    if tee_nodes and 0 <= values["tee_index"] < len(tee_options):
        tee_symbol = tee_options[values["tee_index"]]
    reference_level = project_levels[values["level_index"]]
    offset_tees = [item for item in tee_plans if abs(item["lift"]) > EPSILON]
    lines = [
        "Planned: {} tray piece(s) on {} detail line(s), {} size(s) x {} "
        "tier(s).".format(len(segments), len(references), len(sizes),
                          values["tiers"]),
        "1st tier: {} at {} FFL {:+g} mm (tier gap {:g} mm).".format(
            REFERENCE_NAMES[values["reference"]], reference_level.Name,
            values["ffl_offset"], values["tier_spacing"]
        ),
        "Lane spacing: {:g} mm, tray type '{}'.".format(
            values["run_spacing"], tray_type_name(tray_type)
        ),
    ]
    if corner_count:
        corner_leg = max(plan["corner_legs"] or [0.0])
        lines.append(
            "Corners: {} - a horizontal bend per lane and tier{}.".format(
                corner_count,
                " (bend leg {:.0f} mm)".format(internal_to_mm(corner_leg))
                if corner_leg > EPSILON else ""
            )
        )
    if tee_nodes:
        lines.append("")
        lines.append("Tee fitting: {}".format(fitting_type_name(tee_symbol)))
        for index, size in enumerate(sizes):
            fit = plan["fits"][index]
            line = (
                "  {:g}x{:g}: tee reach {:.0f} mm (main) / {:.0f} mm "
                "(branch)".format(
                    size[0], size[1], internal_to_mm(fit["main_arm"]),
                    internal_to_mm(fit["branch_arm"])
                )
            )
            if fit["bend_leg"] > EPSILON:
                line += ", vertical bend {:.0f} mm".format(
                    internal_to_mm(fit["bend_leg"])
                )
            lines.append(line)
        if crossing["enabled"]:
            lines.append(
                "Tee junctions: {} - {} lane tee(s) offset {} by {:g} mm per "
                "level ({:g} deg bends, {:g} mm clearance).".format(
                    len(tee_nodes), len(offset_tees),
                    DIRECTION_NAMES[crossing["mode"]], values["step"],
                    values["angle"], values["clearance"]
                )
            )
        else:
            lines.append(
                "Tee junctions: {} - crossing offsets are OFF.".format(
                    len(tee_nodes)
                )
            )
    lines.append("")
    if values.get("comments_on"):
        comment_base = base_comment_tokens(
            values, tray_type, reference_level, tee_symbol
        )
        lines.append(
            u'Comments on {}: "{}" (first tray; tokens change per element, '
            u'existing text {}).'.format(
                COMMENT_TARGET_NAMES[values["comments_target"]],
                format_comment(values["comments_text"], comment_base),
                COMMENT_MODE_NAMES[values["comments_mode"]]
            )
        )
    else:
        lines.append("Comments: not written.")
    lines.append("")
    lines.append("Checks")
    lines.append(
        "  Existing services: {} - bounding boxes, {:g} mm clearance, "
        "linked models {}.".format(
            len(clashes) or "none", clearance_mm,
            "included" if include_links else "not checked"
        )
    )
    lines.append("  Tray to tray    : {}.".format(len(internal) or "none"))
    return lines


class ClashPreviewWindow(forms.WPFWindow):
    """Tabbed preview of the planned trays and everything they clash with."""

    def __init__(self, plan, values, internal, clashes, clearance_mm,
                 include_links):
        forms.WPFWindow.__init__(self, CLASH_XAML, literal_string=True)
        self.plan = plan
        self.values = values
        self.segments = plan["segments"]
        self.internal = internal
        self.clashes = clashes
        self.clearance_mm = clearance_mm
        self.include_links = include_links
        self.choice = "cancel"
        try:
            self.MaxHeight = SystemParameters.WorkArea.Height - 20
        except Exception:
            pass
        self.tbServiceClearance.Text = "{:g}".format(clearance_mm)
        self.cbIncludeLinks.IsChecked = include_links
        self.btnRecheck.Click += self.on_recheck
        self.btnCopy.Click += self.on_copy
        self.btnCreate.Click += self.on_create
        self.btnEdit.Click += self.on_edit
        self.btnCancel.Click += self.on_cancel
        self.refresh()

    # ----- filling the tabs --------------------------------------------------
    def refresh(self):
        """Re-draw the headers, both lists and the overall verdict."""
        self.tbOverview.Text = "\n".join(preview_lines(
            self.plan, self.values, self.internal, self.clashes,
            self.clearance_mm, self.include_links
        ))
        self.tabOverview.Header = "Overview"
        self.tabServices.Header = "Existing services ({})".format(
            len(self.clashes)
        )
        self.tabInternal.Header = "Tray crossings ({})".format(
            len(self.internal)
        )
        self.tbHeadline.Text = (
            "{} tray piece(s) planned - nothing is created until you say so."
            .format(len(self.segments))
        )
        self.show_services()
        self.show_internal()
        self.show_verdict()

    def show_verdict(self):
        clear = not self.clashes and not self.internal
        self.brdStatus.Style = self.FindResource(
            "StatusGood" if clear else "StatusBad"
        )
        self.tbStatus.Style = self.FindResource("RowGood" if clear
                                                else "RowBad")
        if clear:
            self.tbStatus.Text = (
                "No clashes found: clear of the existing services and of the "
                "other planned trays."
            )
            return
        parts = []
        if self.clashes:
            parts.append("{} existing service(s)".format(len(self.clashes)))
        if self.internal:
            parts.append("{} tray crossing(s)".format(len(self.internal)))
        self.tbStatus.Text = (
            "Clashes with {} - check the tabs before creating.".format(
                " and ".join(parts)
            )
        )

    def show_services(self):
        self.spServices.Children.Clear()
        self.tbServiceStatus.Text = (
            "{} existing service(s) overlap the planned trays "
            "({:g} mm clearance, linked models {}).".format(
                len(self.clashes), self.clearance_mm,
                "included" if self.include_links else "not checked"
            )
        )
        if not self.clashes:
            self.spServices.Children.Add(self.make_message_row(
                "No bounding-box overlap with the existing services.",
                "RowCardGood", "RowGood"
            ))
            return
        for record in self.clashes[:MAX_PREVIEW_ROWS]:
            self.spServices.Children.Add(self.make_service_row(record))
        if len(self.clashes) > MAX_PREVIEW_ROWS:
            self.spServices.Children.Add(self.make_message_row(
                "... and {} more - use Copy report for the full list.".format(
                    len(self.clashes) - MAX_PREVIEW_ROWS
                ),
                "RowCardBad", "RowBad"
            ))

    def show_internal(self):
        self.spInternal.Children.Clear()
        if not self.internal:
            self.spInternal.Children.Add(self.make_message_row(
                "Every planned crossing has enough vertical clearance.",
                "RowCardGood", "RowGood"
            ))
            return
        for item in self.internal[:MAX_PREVIEW_ROWS]:
            self.spInternal.Children.Add(self.make_internal_row(item))
        if len(self.internal) > MAX_PREVIEW_ROWS:
            self.spInternal.Children.Add(self.make_message_row(
                "... and {} more.".format(
                    len(self.internal) - MAX_PREVIEW_ROWS
                ),
                "RowCardBad", "RowBad"
            ))

    # ----- rows ---------------------------------------------------------------
    def make_message_row(self, message, card_style, text_style):
        card = Border()
        card.Style = self.FindResource(card_style)
        label = TextBlock()
        label.Style = self.FindResource(text_style)
        label.Text = message
        card.Child = label
        return card

    def make_service_row(self, record):
        """One existing service: what it is, how deep, and where to look."""
        card = Border()
        card.Style = self.FindResource("RowCardBad")
        grid = Grid()
        for spec in (GridLength(1, GridUnitType.Star), GridLength(150),
                     GridLength(66), GridLength(62)):
            column = ColumnDefinition()
            column.Width = spec
            grid.ColumnDefinitions.Add(column)

        details = StackPanel()
        title = TextBlock()
        title.Style = self.FindResource("RowTitle")
        title.Text = u"{} - {}".format(record["category"], record["name"])
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        meta.Text = u"ID {} ({})  |  {} tray piece(s): {}".format(
            record["id"],
            u"in {}".format(record["link"]) if record["link"]
            else "this model",
            len(record["pieces"]), self.piece_labels(record["pieces"])
        )
        details.Children.Add(title)
        details.Children.Add(meta)

        overlap = TextBlock()
        overlap.Style = self.FindResource("RowBad")
        overlap.Text = u"overlap\n{}".format(
            describe_overlap(record["overlap"])
        )

        select_button = Button()
        select_button.Content = "Select"
        select_button.Style = self.FindResource("SmallButton")
        zoom_button = Button()
        zoom_button.Content = "Zoom to"
        zoom_button.Style = self.FindResource("SmallButton")
        if record["link"]:
            # Linked elements belong to the link document: this model cannot
            # select them, so say where they live instead.
            for button in (select_button, zoom_button):
                button.IsEnabled = False
                button.ToolTip = (
                    "In the linked model '{}' - open that model to select "
                    "it.".format(record["link"])
                )
        else:
            select_button.ToolTip = "Select this element in Revit"
            zoom_button.ToolTip = "Zoom the active view to this element"
            select_button.Click += self.focus_handler(record, False)
            zoom_button.Click += self.focus_handler(record, True)

        for column, element in enumerate(
                (details, overlap, select_button, zoom_button)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        return card

    def make_internal_row(self, item):
        """One tray-to-tray crossing with too little vertical separation."""
        first, second, separation, needed = item
        card = Border()
        card.Style = self.FindResource("RowCardBad")
        grid = Grid()
        for spec in (GridLength(1, GridUnitType.Star), GridLength(210)):
            column = ColumnDefinition()
            column.Width = spec
            grid.ColumnDefinitions.Add(column)

        details = StackPanel()
        title = TextBlock()
        title.Style = self.FindResource("RowTitle")
        title.Text = u"{}  x  {}".format(
            describe_segment(first), describe_segment(second)
        )
        meta = TextBlock()
        meta.Style = self.FindResource("RowMeta")
        meta.Text = "Both planned by this tool - they cross in plan."
        details.Children.Add(title)
        details.Children.Add(meta)

        numbers = TextBlock()
        numbers.Style = self.FindResource("RowBad")
        numbers.Text = "{:.0f} mm apart, {:.0f} mm needed ({:.0f} mm short)".format(
            internal_to_mm(separation), internal_to_mm(needed),
            internal_to_mm(needed - separation)
        )

        for column, element in enumerate((details, numbers)):
            Grid.SetColumn(element, column)
            grid.Children.Add(element)
        card.Child = grid
        return card

    @staticmethod
    def piece_labels(pieces, limit=3):
        labels = [describe_segment(piece) for piece in pieces[:limit]]
        if len(pieces) > limit:
            labels.append("+{} more".format(len(pieces) - limit))
        return ", ".join(labels)

    # ----- buttons ------------------------------------------------------------
    def focus_handler(self, record, zoom):
        def handler(sender, args):
            self.focus_element(record, zoom)
        return handler

    def focus_element(self, record, zoom):
        """Select (and optionally zoom to) the clashing element in Revit."""
        try:
            element_ids = List[DB.ElementId]()
            element_ids.Add(record["element"].Id)
            uidoc.Selection.SetElementIds(element_ids)
            if zoom:
                uidoc.ShowElements(record["element"].Id)
            self.tbServiceStatus.Text = (
                "{} (ID {}) {} in Revit - this window is in the way, move or "
                "close it to look.".format(
                    record["name"], record["id"],
                    "zoomed to and selected" if zoom else "selected"
                )
            )
        except Exception as error:
            self.tbServiceStatus.Text = (
                "Revit could not show ID {}: {}".format(record["id"], error)
            )

    def report_text(self):
        """The whole preview as text, for the clipboard."""
        lines = preview_lines(
            self.plan, self.values, self.internal, self.clashes,
            self.clearance_mm, self.include_links
        )
        lines.append("")
        lines.append("EXISTING SERVICES ({}, {:g} mm clearance, linked "
                     "models {})".format(
                         len(self.clashes), self.clearance_mm,
                         "included" if self.include_links
                         else "not checked"))
        if not self.clashes:
            lines.append("  none")
        for record in self.clashes:
            lines.append(
                u"  - {} {} (ID {}{}): overlap {}, {} tray piece(s): "
                u"{}".format(
                    record["category"], record["name"], record["id"],
                    u", in {}".format(record["link"]) if record["link"]
                    else "", describe_overlap(record["overlap"]),
                    len(record["pieces"]),
                    self.piece_labels(record["pieces"], 6)
                )
            )
        lines.append("")
        lines.append("TRAY CROSSINGS ({})".format(len(self.internal)))
        if not self.internal:
            lines.append("  none")
        for first, second, separation, needed in self.internal:
            lines.append(
                "  - {} x {}: {:.0f} mm apart, {:.0f} mm needed".format(
                    describe_segment(first), describe_segment(second),
                    internal_to_mm(separation), internal_to_mm(needed)
                )
            )
        return "\n".join(lines)

    def on_recheck(self, sender, args):
        """Run the service check again with the clearance / link choice."""
        try:
            clearance = float(self.tbServiceClearance.Text.strip())
        except ValueError:
            self.tbServiceStatus.Text = "Clearance must be a number in mm."
            return
        if math.isnan(clearance) or math.isinf(clearance) or clearance < 0:
            self.tbServiceStatus.Text = "Clearance must be 0 mm or more."
            return
        include_links = self.cbIncludeLinks.IsChecked == True
        try:
            self.clashes = service_clashes(
                self.segments, clearance, include_links
            )
        except Exception as error:
            self.tbServiceStatus.Text = "The check failed: {}".format(error)
            return
        self.clearance_mm = clearance
        self.include_links = include_links
        PREVIEW_STATE["clearance"] = clearance
        PREVIEW_STATE["links"] = include_links
        self.refresh()
        self.tabServices.IsSelected = True

    def on_copy(self, sender, args):
        try:
            Clipboard.SetText(self.report_text())
            self.tbServiceStatus.Text = "Report copied to the clipboard."
        except Exception as error:
            self.tbServiceStatus.Text = (
                "Could not copy the report: {}".format(error)
            )

    def on_create(self, sender, args):
        self.choice = "create"
        self.Close()

    def on_edit(self, sender, args):
        self.choice = "edit"
        self.Close()

    def on_cancel(self, sender, args):
        self.choice = "cancel"
        self.Close()


def show_clash_preview(plan, values):
    """Run the clash checks and show the preview.

    Returns 'create', 'edit' (go back to the settings dialog) or 'cancel'."""
    segments = plan["segments"]
    internal = internal_crossings(segments)
    clearance_mm = PREVIEW_STATE["clearance"]
    include_links = PREVIEW_STATE["links"]
    try:
        clashes = service_clashes(segments, clearance_mm, include_links)
    except Exception:
        # A link that cannot be read must not stop the preview.
        include_links = False
        PREVIEW_STATE["links"] = False
        clashes = service_clashes(segments, clearance_mm, False)
    window = ClashPreviewWindow(
        plan, values, internal, clashes, clearance_mm, include_links
    )
    window.ShowDialog()
    return window.choice


# =============================================================================
# MAIN
# =============================================================================
# ---- 1. What is loaded in the project -----------------------------------------
tray_types = list(
    DB.FilteredElementCollector(doc).OfClass(CableTrayType).ToElements()
)
tray_types.sort(key=lambda tray_type: tray_type_name(tray_type).lower())
if not tray_types:
    forms.alert("No cable tray types are available in this project.",
                title=TITLE, warn_icon=True)
    raise SystemExit

fitting_symbols = list(
    DB.FilteredElementCollector(doc)
    .OfCategory(DB.BuiltInCategory.OST_CableTrayFitting)
    .OfClass(DB.FamilySymbol)
    .ToElements()
)
tee_options = build_tee_options(fitting_symbols, tray_types)

# Levels (lowest first) - the 1st tier is placed at a level FFL + offset.
project_levels = sorted(
    DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements(),
    key=level_elevation
)
if not project_levels:
    forms.alert("No level is available for creating cable trays.",
                title=TITLE, warn_icon=True)
    raise SystemExit
current_view_level = doc.ActiveView.GenLevel     # None in 3D / sections

# ---- 2. Dialog -------------------------------------------------------------------
stored_settings, user_config = load_settings()
values = run_form(stored_settings, u"Select detail lines  →")

# ---- 3. Pick detail lines ----------------------------------------------------------
try:
    references = uidoc.Selection.PickObjects(
        ObjectType.Element,
        DetailLineFilter(),
        "Select straight detail lines, then click Finish"
    )
except OperationCanceledException:
    raise SystemExit
if not references:
    forms.alert("Select at least one straight detail line.",
                title=TITLE, warn_icon=True)
    raise SystemExit

picked_segments = []
for reference in references:
    detail_line = doc.GetElement(reference.ElementId)
    curve = detail_line.Location.Curve
    picked_segments.append((curve.GetEndPoint(0), curve.GetEndPoint(1)))

# Run every chain of connected lines in one direction so each tray size
# stays on the same side round the corners (nested bends, no crossing).
line_segments, line_flipped = orient_line_chains(picked_segments)
tee_nodes = tee_junctions(line_segments)

# Corners where exactly two lines meet end to end at an angle -> bends.
line_links = endpoint_links(line_segments)
corner_count = 0
for (line_index, end), partners in line_links.items():
    if len(partners) != 1:
        continue
    other_index = partners[0][0]
    alignment = abs(
        horizontal_unit(*line_segments[line_index]).DotProduct(
            horizontal_unit(*line_segments[other_index])
        )
    )
    if alignment <= 0.97:
        corner_count += 1
corner_count //= 2      # each corner was counted from both lines

# The fitting probes run on the chosen level (always rolled back).
first_point = line_segments[0][0]
probe_level = project_levels[values["level_index"]]
probe = FittingProbe(
    probe_level,
    DB.XYZ(first_point.X, first_point.Y,
           level_elevation(probe_level) + mm_to_internal(values["ffl_offset"]))
)

# ---- 4. Check dimensions against real fittings; suggest + confirm -------------
APPLY_CHOICE = "Apply the suggested values and continue"
EDIT_CHOICE = "Edit the settings"
CANCEL_CHOICE = "Cancel"
RECHECK_TEXT = u"Re-check and continue  →"

plan = None
# Each pass: check the dimensions, then show the clash preview. "Back to
# settings" in either dialog comes round again, so the budget covers both.
for attempt in range(12):
    outcome = evaluate(values, probe)
    if outcome["fatal"]:
        choice = forms.alert(
            outcome["fatal"],
            title=TITLE,
            warn_icon=True,
            options=[EDIT_CHOICE, CANCEL_CHOICE]
        )
        if choice == EDIT_CHOICE:
            values = run_form(values_to_settings(values), RECHECK_TEXT, values)
            continue
        raise SystemExit
    if outcome["changes"]:
        choice = forms.alert(
            format_changes(outcome["changes"]),
            title="Cable Tray - Suggested Dimensions",
            warn_icon=True,
            options=[APPLY_CHOICE, EDIT_CHOICE, CANCEL_CHOICE]
        )
        if choice == APPLY_CHOICE:
            values = outcome["proposed"]
            save_settings(user_config, values_to_settings(values))
            continue
        if choice == EDIT_CHOICE:
            values = run_form(
                values_to_settings(outcome["proposed"]), RECHECK_TEXT,
                outcome["proposed"]
            )
            continue
        raise SystemExit
    plan = outcome["plan"]

    # ---- 5. Tabbed clash preview (read-only) -------------------------------
    choice = show_clash_preview(plan, values)
    if choice == "create":
        break
    if choice == "edit":
        values = run_form(values_to_settings(values), RECHECK_TEXT, values)
        plan = None
        continue
    raise SystemExit

if plan is None:
    forms.alert(
        "No workable dimensions were found after several attempts. Check "
        "the detail lines and the tray type fittings.",
        title=TITLE, warn_icon=True
    )
    raise SystemExit

tray_type = tray_types[values["type_index"]]
sizes = values["sizes"]
tee_symbol = None
if tee_nodes and 0 <= values["tee_index"] < len(tee_options):
    tee_symbol = tee_options[values["tee_index"]]
crossing = plan["crossing"]
segments = plan["segments"]
tee_plans = plan["tee_plans"]

# ---- 5b. Values the creation step needs (the preview above was confirmed) -----
offset_tees = [item for item in tee_plans if abs(item["lift"]) > EPSILON]
reference_level = project_levels[values["level_index"]]
# Token values shared by every element - also the example shown in the
# preview and the starting point for each element's own comment.
comment_base = base_comment_tokens(
    values, tray_type, reference_level, tee_symbol
)

# ---- 6. Create everything (MODIFIES THE MODEL) ---------------------------------
created_segments = []
created_fittings = []       # fittings created here, for their Comments
comment_count = 0
comment_failures = 0
type_notes = []
tee_count = 0
tee_failures = []
bend_count = 0
bend_attempts = 0
bend_failures = []
riser_failures = 0
elbow_count = 0
elbow_failures = []
elbow_attempts_by_size = [0 for size in sizes]
elbow_counts_by_size = [0 for size in sizes]
join_count = 0
join_failures = []
warnings = WarningSwallower()

transaction = DB.Transaction(doc, "Cable Tray Auto Route Pro")
transaction.Start()
failure_options = transaction.GetFailureHandlingOptions()
failure_options.SetFailuresPreprocessor(warnings)
transaction.SetFailureHandlingOptions(failure_options)
try:
    # 6a. Fittings on the tray type (Revit uses them for every fitting).
    if tee_symbol is not None and assign_fitting(tray_type, "tee", tee_symbol):
        type_notes.append("Tee fitting of tray type '{}' set to '{}'.".format(
            tray_type_name(tray_type), fitting_type_name(tee_symbol)
        ))
    if values.get("bends"):
        for role, symbol in zip(("inside_bend", "outside_bend"),
                                values["bends"]):
            if assign_fitting(tray_type, role, symbol):
                type_notes.append("{} of tray type '{}' set to '{}'.".format(
                    "Vertical Inside Bend" if role == "inside_bend"
                    else "Vertical Outside Bend",
                    tray_type_name(tray_type), fitting_type_name(symbol)
                ))
    if values.get("hbend") and assign_fitting(
            tray_type, "horizontal_bend", values["hbend"]):
        type_notes.append("Horizontal Bend of tray type '{}' set to '{}'.".format(
            tray_type_name(tray_type), fitting_type_name(values["hbend"])
        ))
    doc.Regenerate()

    # 6b. One cable tray per straight / offset piece.
    segments_by_route = {}
    for segment in segments:
        segment["element"] = create_tray(
            tray_type, segment["level"], segment["start"], segment["end"],
            segment["width"], segment["height"]
        )
        segments_by_route.setdefault(segment["route_key"], []).append(segment)
        created_segments.append(segment)
    doc.Regenerate()

    # 6c. 90-degree risers: turn them to line up with the trays they join.
    for segment in created_segments:
        if is_vertical_segment(segment):
            if not orient_vertical_tray(segment["element"], segment["direction"]):
                riser_failures += 1
    doc.Regenerate()

    # 6d. Tees - Revit inserts the tray type's tee and SIZES it.
    tee_tolerance = mm_to_internal(5)
    for tee_plan in tee_plans:
        centre = tee_plan["center"]
        label = "{:g}x{:g} mm, tier {}, junction {}".format(
            tee_plan["width"], tee_plan["height"],
            tee_plan["tier_index"] + 1, tee_plan["junction_index"] + 1
        )
        main_connectors = connectors_at(
            segments_by_route.get(tee_plan["main_key"], []), centre,
            tee_tolerance
        )
        branch_connectors = connectors_at(
            segments_by_route.get(tee_plan["branch_key"], []), centre,
            tee_tolerance
        )
        if len(main_connectors) < 2 or not branch_connectors:
            if len(tee_failures) < 8:
                tee_failures.append(
                    "{}: tray ends do not meet at the tee point".format(label)
                )
            continue
        tee_transaction = DB.SubTransaction(doc)
        tee_transaction.Start()
        try:
            tee_fitting = doc.Create.NewTeeFitting(
                main_connectors[0], main_connectors[1], branch_connectors[0]
            )
            if tee_transaction.Commit() != DB.TransactionStatus.Committed:
                raise Exception("Revit did not commit the tee fitting.")
            tee_count += 1
            created_fittings.append(fitting_note(
                tee_fitting, "tee", tee_plan["width"], tee_plan["height"],
                tee_plan["main_key"][1], tee_plan["tier_index"],
                tee_plan["main_key"][0], internal_to_mm(tee_plan["lift"])
            ))
        except Exception as error:
            if tee_transaction.GetStatus() == DB.TransactionStatus.Started:
                tee_transaction.RollBack()
            if len(tee_failures) < 8:
                tee_failures.append("{}: {}".format(label, error))
    doc.Regenerate()

    # 6e. Vertical offset bends (the S-bends of the crossing lanes).
    bend_tolerance = mm_to_internal(5)
    for route_key in plan["route_order"]:
        pieces = segments_by_route.get(route_key, [])
        for index in range(len(pieces) - 1):
            first = pieces[index]
            second = pieces[index + 1]
            if first["end_kind"] != "bend" or second["start_kind"] != "bend":
                continue
            if first["end"].DistanceTo(second["start"]) > bend_tolerance:
                continue
            bend_attempts += 1
            first_connector = nearest_free_connector(
                first["element"], first["end"]
            )
            second_connector = nearest_free_connector(
                second["element"], second["start"]
            )
            if first_connector is None or second_connector is None:
                if len(bend_failures) < 8:
                    bend_failures.append(
                        "{}: no free tray end at the bend".format(
                            describe_segment(first)
                        )
                    )
                continue
            bend_transaction = DB.SubTransaction(doc)
            bend_transaction.Start()
            try:
                bend_fitting = doc.Create.NewElbowFitting(
                    first_connector, second_connector
                )
                if bend_transaction.Commit() != DB.TransactionStatus.Committed:
                    raise Exception("Revit did not commit the bend fitting.")
                bend_count += 1
                created_fittings.append(fitting_note(
                    bend_fitting, "bend", first["width"], first["height"],
                    first["size_index"], first["tier_index"],
                    first["line_index"],
                    segment_lift_mm(plan["routes"], first, first["end"])
                ))
            except Exception as error:
                if bend_transaction.GetStatus() == DB.TransactionStatus.Started:
                    bend_transaction.RollBack()
                if len(bend_failures) < 8:
                    bend_failures.append("{}: {}".format(
                        describe_segment(first), error
                    ))

    # 6f. Corner bends / straight joins where detail lines meet end to end.
    #     Every lane end is matched to the SAME lane (size + tier) of the
    #     other line - the planner already made those two ends meet exactly.
    doc.Regenerate()
    link_tolerance = mm_to_internal(25)
    routes = plan["routes"]
    for route_key in plan["route_order"]:
        route = routes[route_key]
        line_index, size_index, tier_index = route_key
        pieces = segments_by_route.get(route_key, [])
        if not pieces:
            continue
        for my_end, link, cap in ((0, route["start_link"], route["start_cap"]),
                                  (1, route["end_link"], route["end_cap"])):
            if link is None or cap is not None:
                continue
            partners = line_links.get((line_index, my_end), [])
            if len(partners) != 1:
                continue
            other_line, other_end = partners[0]
            if other_line < line_index:
                continue                        # each corner only once
            other_key = (other_line, size_index, tier_index)
            other_route = routes.get(other_key)
            other_pieces = segments_by_route.get(other_key, [])
            if not other_pieces or other_route[
                    "start_cap" if other_end == 0 else "end_cap"] is not None:
                continue
            my_piece = pieces[0] if my_end == 0 else pieces[-1]
            my_point = my_piece["start"] if my_end == 0 else my_piece["end"]
            other_piece = other_pieces[0] if other_end == 0 else other_pieces[-1]
            other_point = (other_piece["start"] if other_end == 0
                           else other_piece["end"])
            label = "{:g}x{:g} mm, tier {}, line {} to {}".format(
                sizes[size_index][0], sizes[size_index][1], tier_index + 1,
                line_index + 1, other_line + 1
            )
            if link == "bend":
                elbow_attempts_by_size[size_index] += 1
            failures = elbow_failures if link == "bend" else join_failures
            if my_point.DistanceTo(other_point) > link_tolerance:
                if len(failures) < 10:
                    failures.append(label + ": the tray ends do not meet")
                continue
            first_connector = nearest_free_connector(my_piece["element"],
                                                     my_point)
            second_connector = nearest_free_connector(other_piece["element"],
                                                      other_point)
            if first_connector is None or second_connector is None:
                if len(failures) < 10:
                    failures.append(label + ": no free tray end")
                continue
            fitting_transaction = DB.SubTransaction(doc)
            fitting_transaction.Start()
            link_fitting = None
            try:
                if link == "bend":
                    link_fitting = doc.Create.NewElbowFitting(
                        first_connector, second_connector
                    )
                else:
                    try:
                        link_fitting = doc.Create.NewUnionFitting(
                            first_connector, second_connector
                        )
                    except Exception:
                        # No union family: a plain connection, no element.
                        first_connector.ConnectTo(second_connector)
                status = fitting_transaction.Commit()
                if status != DB.TransactionStatus.Committed:
                    raise Exception("Revit did not commit the fitting.")
                if link == "bend":
                    elbow_count += 1
                    elbow_counts_by_size[size_index] += 1
                else:
                    join_count += 1
                if link_fitting is not None:
                    created_fittings.append(fitting_note(
                        link_fitting, "corner" if link == "bend" else "join",
                        sizes[size_index][0], sizes[size_index][1],
                        size_index, tier_index, line_index,
                        segment_lift_mm(plan["routes"], my_piece, my_point)
                    ))
            except Exception as error:
                if (fitting_transaction.GetStatus()
                        == DB.TransactionStatus.Started):
                    fitting_transaction.RollBack()
                if len(failures) < 10:
                    failures.append("{}: {}".format(label, error))

    # 6g. Comments on everything created (from the dialog's Comments card).
    #     Only the elements this tool made are touched; each gets its own
    #     tokens, so one template labels every tray, tee and bend.
    if values.get("comments_on") and values.get("comments_text"):
        comment_template = values["comments_text"]
        comment_mode = values.get("comments_mode", "replace")
        commented = list(created_segments)
        if values.get("comments_target") == "all":
            commented += created_fittings
        for item in commented:
            if "route_key" in item:         # a tray piece
                part = segment_comment_part(item)
                lift_mm = segment_lift_mm(plan["routes"], item)
            else:                           # a fitting
                part = item["part"]
                lift_mm = item["lift"]
            tokens = element_comment_tokens(
                comment_base, item["width"], item["height"],
                item["size_index"], item["tier_index"], item["line_index"],
                part, lift_mm
            )
            if write_comment(item["element"],
                             format_comment(comment_template, tokens),
                             comment_mode):
                comment_count += 1
            else:
                comment_failures += 1

    transaction.Commit()
except Exception as error:
    if transaction.GetStatus() == DB.TransactionStatus.Started:
        transaction.RollBack()
    forms.alert(
        "Could not create the cable trays - nothing was changed.\n\n"
        "{}".format(error),
        title=TITLE,
        warn_icon=True
    )
    raise SystemExit

# ---- 7. Result summary --------------------------------------------------------------
summary = [
    "{} cable tray piece(s) created from {} detail line(s).".format(
        len(created_segments), len(references)
    ),
    "Sizes: {}  |  Tiers: {} (gap {:g} mm)".format(
        ", ".join("{:g}x{:g}".format(w, h) for w, h in sizes),
        values["tiers"], values["tier_spacing"]
    ),
    "1st tier: {} at {} FFL {:+g} mm.".format(
        REFERENCE_NAMES[values["reference"]], reference_level.Name,
        values["ffl_offset"]
    ),
    "",
]
if tee_nodes:
    summary.append("Tee fittings: {}/{} placed at {} T-junction(s) ({}).".format(
        tee_count, len(tee_plans), len(tee_nodes),
        fitting_type_name(tee_symbol)
    ))
    if crossing["enabled"]:
        summary.append(
            "Crossing offsets: {} lane tee(s) offset {} ({:g} mm step, "
            "{:g} deg bends).".format(
                len(offset_tees),
                DIRECTION_NAMES[crossing["mode"]],
                values["step"], values["angle"]
            )
        )
        summary.append("Vertical offset bends: {}/{} placed.".format(
            bend_count, bend_attempts
        ))
    else:
        summary.append("Crossing offsets: OFF")
if corner_count:
    summary.append("Corner bends: {}/{} placed ({}).".format(
        elbow_count, sum(elbow_attempts_by_size),
        "; ".join(
            "{:g}x{:g}: {}/{}".format(
                size[0], size[1],
                elbow_counts_by_size[index], elbow_attempts_by_size[index]
            )
            for index, size in enumerate(sizes)
        )
    ))
if join_count or join_failures:
    summary.append("Straight joins between lines: {} connected.".format(
        join_count
    ))
if values.get("comments_on"):
    summary.append("")
    summary.append(
        u'Comments written on {} element(s) ({}), e.g. "{}".'.format(
            comment_count,
            COMMENT_TARGET_NAMES[values["comments_target"]],
            format_comment(values["comments_text"], comment_base)
        )
    )
    if comment_failures:
        summary.append(
            "  {} element(s) have no writable Comments parameter.".format(
                comment_failures
            )
        )
if type_notes:
    summary.append("")
    summary.extend(type_notes)
if tee_failures:
    summary.append("")
    summary.append("Some tees could not be placed:")
    summary.extend("  - " + item for item in tee_failures)
if bend_failures:
    summary.append("")
    summary.append(
        "Some offset bends could not be placed - check the tray type's "
        "'Vertical Inside Bend' / 'Vertical Outside Bend' fittings:"
    )
    summary.extend("  - " + item for item in bend_failures)
if riser_failures:
    summary.append(
        "{} vertical riser(s) could not be re-oriented; check their "
        "rotation.".format(riser_failures)
    )
if elbow_failures:
    summary.append("")
    summary.append(
        "Some corner bends could not be created - check the tray type's "
        "'Horizontal Bend' fitting:"
    )
    summary.extend("  - " + item for item in elbow_failures)
if join_failures:
    summary.append("")
    summary.append("Some straight joins could not be connected:")
    summary.extend("  - " + item for item in join_failures)
if warnings.count:
    summary.append("")
    summary.append(
        "{} Revit warning(s) (e.g. overlapping elements) were dismissed "
        "automatically.".format(warnings.count)
    )
forms.alert("\n".join(summary), title=TITLE)
