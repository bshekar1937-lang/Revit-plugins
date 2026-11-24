# -*- coding: utf-8 -*-
"""Check the IP-rating rule engine and the change logic, without Revit."""
import io, sys

HERE = "/tmp/claude-0/-home-user-Revit-plugins/1c556330-39d3-5fb3-96e8-16f663c5e2fa/scratchpad/"
# Read the assembled script, so the checks run against what ships.
part1 = io.open("/home/user/Revit-plugins/IPRating_ByLocation.py",
                encoding="utf-8").read()
part2 = part1
failures = []


def true(what, value):
    if not value:
        failures.append(what)
    print("   [%s] %s" % ("PASS" if value else "FAIL", what))


def same(what, actual, expected):
    ok = actual == expected
    if not ok:
        failures.append(what)
    print("   [%s] %s: %r%s" % ("PASS" if ok else "FAIL", what, actual,
                                "" if ok else " (expected %r)" % (expected,)))


# ----- the rule engine, taken straight from the script ----------------------
ns = {}
rules_block = part1[part1.index("class Rule(object):"):
                    part1.index("# Finding the room an item stands in")]
exec(compile(rules_block, "rules", "exec"), ns)
DEFAULT_RULES = part1[part1.index('DEFAULT_RULES = u"""') + len('DEFAULT_RULES = u"""'):]
DEFAULT_RULES = DEFAULT_RULES[:DEFAULT_RULES.index('"""')]

print("=== 1. The default rule list parses")
rules, problems = ns["parse_rules"](DEFAULT_RULES)
same("no problems", problems, [])
true("%d rules were read" % len(rules), len(rules) > 15)
true("the last one is the catch-all", rules[-1].catch_all)

print()
print("=== 2. Rooms are matched to the right rating")
cases = [
    ("E12 Main Switchroom", "IP31"),
    ("B04 Plant Room", "IP54"),
    ("G01 Office", "IP20"),
    ("R01 Roof Terrace", "IP66"),
    ("W02 Shower Room", "IP65"),
    ("K01 Main Kitchen", "IP55"),
    ("T03 Toilet Lobby", "IP44"),
    ("P01 Basement Car Park", "IP54"),
    ("S01 Electrical Riser", "IP31"),
    ("L01 Swimming Pool Hall", "IP68"),
    ("U01 Sump Pit", "IP67"),
    ("C01 Comms Room", "IP31"),
    ("D01 Reception", "IP20"),
]
for room, expected in cases:
    rule = ns["match_rule"](rules, room)
    got = rule.value if rule else None
    same("%-24s" % room, got, expected)

print()
print("=== 2b. A word is matched at the start of a word, not inside one")
tricky = [
    ("M01 District Cooling Room", "IP54"),   # not comms, despite "ict"
    ("E02 Switchrooms", "IP31"),             # plural still matches
    ("B03 Plantroom", "IP54"),               # run together still matches
    ("G04 Restaurant", "IP20"),              # "aura"? no token inside words
    ("L05 Scullery", "IP55"),                # whole word at the start
    ("T06 WC Lobby", "IP44"),                # short token, real word
]
for room, expected in tricky:
    rule = ns["match_rule"](rules, room)
    same("%-26s" % room, rule.value if rule else None, expected)

print()
print("=== 3. First match wins, and case does not matter")
ordered, _ = ns["parse_rules"](u"""
switchroom => IP31
room       => IP20
""")
same("the specific line wins over the general one",
     ns["match_rule"](ordered, "E1 SWITCHROOM").value, "IP31")
same("and the general one still catches the rest",
     ns["match_rule"](ordered, "Meeting Room").value, "IP20")
same("upper case input matches lower case rules",
     ns["match_rule"](ordered, "PLANT SWITCHROOM 2").value, "IP31")
true("a room nothing matches gives nothing",
     ns["match_rule"](ordered, "Atrium") is None)

print()
print("=== 4. Bad rule lines are reported with their line number")
bad, problems = ns["parse_rules"](u"""# a note
kitchen => IP55
this line has no arrow
plant =>
 => IP20
* => IP20
""")
true("two good rules survived", len(bad) == 2)
true("three problems found", len(problems) == 3)
for problem in problems:
    print("      %s" % problem)
true("they name the lines", all(("Line 4" in p or "Line 5" in p
                                 or "Line 3" in p) for p in problems))

print()
print("=== 5. What gets written, and what is left alone")
stub = dict(ns)
stub.update({
    "doc": None,
    "element_id_value": lambda i: i,
    "element_name": lambda e: e["name"],
    "family_and_type": lambda e: e["type"],
    "level_name": lambda e: "L1",
    "parameter_text": lambda p: p if isinstance(p, str) else "",
    "find_parameter": lambda e, n, s: e.get("param"),
})


class Element(dict):
    """Stands in for a Revit element: Id and GetTypeId only."""
    @property
    def Id(self): return self["id"]
    def GetTypeId(self): return self["type_id"]


changes_block = part2[part2.index("STATUS_WRITE = "):
                      part2.index("# SETUP DIALOG (WPF)")]
changes_block = changes_block[:changes_block.index("def collect_elements(")] \
    + changes_block[changes_block.index("def build_changes("):]
exec(compile(changes_block, "changes", "exec"), stub)


class Finder(object):
    def __init__(self, rooms): self.rooms = rooms
    def find(self, element):
        room = self.rooms.get(element["name"])
        if room is None:
            return None
        return {"label": room, "haystack": room, "name": room, "number": "",
                "kind": "Room", "source": "", "how": "its own point"}


def run(elements, rooms, **options):
    values = {"parameter": "IP Rating", "parameter_scope": "instance",
              "overwrite": False, "no_room": "skip", "no_room_value": "IP66"}
    values.update(options)
    return stub["build_changes"](elements, rules, Finder(rooms), values)


items = [
    Element(id=1, name="DB-01", type="Board : 100A", type_id=900, param=""),
    Element(id=2, name="DB-02", type="Board : 100A", type_id=900,
            param="IP54"),
    Element(id=3, name="DB-03", type="Board : 100A", type_id=900,
            param="IP44"),
    Element(id=4, name="DB-04", type="Board : 100A", type_id=900, param=None),
    Element(id=5, name="DB-05", type="Board : 100A", type_id=900, param=""),
]
rooms = {"DB-01": "B01 Plant Room", "DB-02": "B02 Plant Room",
         "DB-03": "G01 Office", "DB-04": "G02 Office"}
result = run(items, rooms)
by_name = dict((c["name"], c) for c in result)
same("empty parameter in a plant room is written",
     (by_name["DB-01"]["status"], by_name["DB-01"]["wanted"]),
     (stub["STATUS_WRITE"], "IP54"))
same("a value that is already right is left alone",
     by_name["DB-02"]["status"], stub["STATUS_SAME"])
same("a different value is kept unless overwrite is on",
     by_name["DB-03"]["status"], stub["STATUS_KEEP"])
same("an item without the parameter is reported",
     by_name["DB-04"]["status"], stub["STATUS_NO_PARAM"])
same("an item with no room is reported, not guessed at",
     by_name["DB-05"]["status"], stub["STATUS_NO_ROOM"])
true("only the writable one is ticked",
     [c["name"] for c in result if c["include"]] == ["DB-01"])

overwritten = run(items, rooms, overwrite=True)
same("with overwrite on, the wrong value is replaced",
     dict((c["name"], c["status"]) for c in overwritten)["DB-03"],
     stub["STATUS_WRITE"])

outside = run(items, rooms, no_room="value", no_room_value="IP66")
outside_by_name = dict((c["name"], c) for c in outside)
same("no room, told to treat as external",
     (outside_by_name["DB-05"]["status"], outside_by_name["DB-05"]["wanted"]),
     (stub["STATUS_WRITE"], "IP66"))

print()
print("=== 6. One type cannot hold two ratings")
shared = [
    Element(id=1, name="A", type="Isolator : 32A", type_id=700, param=""),
    Element(id=2, name="B", type="Isolator : 32A", type_id=700, param=""),
    Element(id=3, name="C", type="Isolator : 63A", type_id=701, param=""),
]
clash = run(shared, {"A": "R1 Roof", "B": "G1 Office", "C": "G2 Office"},
            parameter_scope="type")
status = dict((c["name"], c["status"]) for c in clash)
same("the shared type is flagged, not written (A)", status["A"],
     stub["STATUS_TYPE_CLASH"])
same("the shared type is flagged, not written (B)", status["B"],
     stub["STATUS_TYPE_CLASH"])
same("a type used in one kind of room is fine", status["C"],
     stub["STATUS_WRITE"])
true("nothing ticked for the clashing type",
     [c["name"] for c in clash if c["include"]] == ["C"])

same("instance scope has no such problem",
     [c["status"] for c in run(shared,
                               {"A": "R1 Roof", "B": "G1 Office",
                                "C": "G2 Office"})],
     [stub["STATUS_WRITE"]] * 3)

print()
print("=== 7. Rule hit counts, for the summary")
counted, _ = ns["parse_rules"](u"plant => IP54\n* => IP20\n")
hits = stub["build_changes"](
    items, counted, Finder(rooms),
    {"parameter": "IP Rating", "parameter_scope": "instance",
     "overwrite": True, "no_room": "skip", "no_room_value": ""})
same("the plant rule was used twice", counted[0].hits, 2)
same("the catch-all caught the offices", counted[1].hits, 2)

print()
if failures:
    print("%d check(s) FAILED: %s" % (len(failures), failures))
    sys.exit(1)
print("All IP rule checks passed.")
