# -*- coding: utf-8 -*-
"""Check the circuit room-name logic without Revit.

Run it with:  python3 -I tests_CircuitRoomNames.py
It reads the shipped script, so it checks what actually ships.
"""
import io, os, re, sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "CircuitRoomNames.py")
src = io.open(SCRIPT, encoding="utf-8").read()
failures = []


def same(what, actual, expected):
    ok = actual == expected
    if not ok:
        failures.append(what)
    print(u"   [%s] %s: %r%s" % ("PASS" if ok else "FAIL", what, actual,
                                 u"" if ok else u" (expected %r)" % (expected,)))


def true(what, value):
    if not value:
        failures.append(what)
    print(u"   [%s] %s" % ("PASS" if value else "FAIL", what))


# ----- the block under test, with Revit stood in for -------------------------
block = src[src.index("# The circuits and the rooms they pass through"):
            src.index("# SETUP DIALOG (WPF)")]


class Thing(dict):
    """Stands in for a Revit element: only Id and GetTypeId are used."""
    @property
    def Id(self): return self["id"]
    def GetTypeId(self): return self.get("type_id", 0)


class System(Thing):
    @property
    def CircuitNumber(self): return self.get("number", "")
    @property
    def PanelName(self): return self.get("panel", "")
    @property
    def LoadName(self): return self.get("load", "")
    @property
    def Elements(self): return self.get("devices", [])
    @property
    def BaseEquipment(self): return self.get("base")
    @property
    def SystemType(self): return self.get("kind", "PowerCircuit")


class Finder(object):
    """Hands back the room a device was put in by the test."""
    def find(self, element):
        return element.get("room")


ns = {
    "re": re, "doc": None, "DB": None, "FilteredElementCollector": None,
    "ElectricalSystem": None,
    "element_id_value": lambda i: i,
    "element_name": lambda e: e.get("name", ""),
    "family_and_type": lambda e: e.get("type", ""),
    "parameter_text": lambda p: p if isinstance(p, str) else u"",
    "find_parameter": lambda e, n, s: e.get("param"),
}
exec(compile(block, "circuits", "exec"), ns)


def room(number, name):
    return {"number": number, "name": name, "label": number + " " + name,
            "kind": "Room", "source": "", "how": "its own point"}


def device(ident, name, where):
    return Thing(id=ident, name=name, type="Socket : SSO", room=where)


def options(**extra):
    values = {"parameter": "Room Names", "name_format": "name",
              "separator": ", ", "order": "number", "show_counts": False,
              "include_panel_room": False, "no_room_text": "",
              "overwrite": True}
    values.update(extra)
    return values


def value_for(devices, **extra):
    values = options(**extra)
    entries, detail, missing = ns["gather_rooms"](devices, Finder(), values)
    return ns["joined_value"](entries, values), entries, missing


# The circuit from the drawing: a socket in the MCCS room, one in the
# corridor and one in the entrance facility room.
MCCS = room("B02-15", "LL MCCS ROOM")
CORRIDOR = room("B02-20", "CORRIDOR")
ENTRANCE = room("B02-31", "ENTRANCE FACILITY ROOM")
GSM = room("B02-10", "LL MAIN GSM ROOM")

print("=== 1. The circuit from the drawing")
circuit = [device(1, "SSO-1", MCCS), device(2, "SSO-2", CORRIDOR),
           device(3, "SSO-3", ENTRANCE)]
text, entries, missing = value_for(circuit)
same("three rooms, in room-number order", text,
     u"LL MCCS ROOM, CORRIDOR, ENTRANCE FACILITY ROOM")
same("nothing was missed", missing, 0)

print()
print("=== 2. Two sockets in one room count once")
repeated = [device(1, "SSO-1", MCCS), device(2, "SSO-2", CORRIDOR),
            device(3, "SSO-3", CORRIDOR), device(4, "SSO-4", CORRIDOR)]
text, entries, missing = value_for(repeated)
same("the room is listed once", text, u"LL MCCS ROOM, CORRIDOR")
same("but the devices are counted", [e["count"] for e in entries], [1, 3])
text, _, _ = value_for(repeated, show_counts=True)
same("and the count can be shown", text, u"LL MCCS ROOM, CORRIDOR (3)")

print()
print("=== 3. How each room is written")
for name_format, expected in (
        ("name", u"LL MCCS ROOM, CORRIDOR"),
        ("number_name", u"B02-15 LL MCCS ROOM, B02-20 CORRIDOR"),
        ("number", u"B02-15, B02-20")):
    text, _, _ = value_for(circuit[:2], name_format=name_format)
    same("%-12s" % name_format, text, expected)

print()
print("=== 4. The order the rooms are listed in")
jumbled = [device(1, "a", ENTRANCE), device(2, "b", MCCS),
           device(3, "c", CORRIDOR)]
same("by number", value_for(jumbled, order="number")[0],
     u"LL MCCS ROOM, CORRIDOR, ENTRANCE FACILITY ROOM")
same("by name", value_for(jumbled, order="name")[0],
     u"CORRIDOR, ENTRANCE FACILITY ROOM, LL MCCS ROOM")
same("as they come on the circuit", value_for(jumbled, order="circuit")[0],
     u"ENTRANCE FACILITY ROOM, LL MCCS ROOM, CORRIDOR")

print()
print("=== 5. Room numbers sort the way a person reads them")
nine = room("B2-9", "STORE 9")
ten = room("B2-10", "STORE 10")
same("B2-9 comes before B2-10",
     value_for([device(1, "a", ten), device(2, "b", nine)])[0],
     u"STORE 9, STORE 10")

print()
print("=== 6. A device with no room")
stranded = [device(1, "SSO-1", MCCS), device(2, "SSO-2", None),
            device(3, "SSO-3", CORRIDOR)]
text, entries, missing = value_for(stranded)
same("it is left out of the value", text, u"LL MCCS ROOM, CORRIDOR")
same("but it is counted so the preview can say so", missing, 1)
text, _, _ = value_for(stranded, no_room_text=u"(no room)")
same("or it can be named in the value", text,
     u"LL MCCS ROOM, CORRIDOR, (no room)")

print()
print("=== 7. The panel's own room goes first, whatever the sort")
with_panel = options(include_panel_room=True)
entries, _, _ = ns["gather_rooms"]([device(1, "a", CORRIDOR),
                                    device(2, "b", ENTRANCE)],
                                   Finder(), with_panel, GSM)
same("the panel room leads", ns["joined_value"](entries, with_panel),
     u"LL MAIN GSM ROOM, CORRIDOR, ENTRANCE FACILITY ROOM")

print()
print("=== 8. Separators")
for separator, expected in ((", ", u"LL MCCS ROOM, CORRIDOR"),
                            (" / ", u"LL MCCS ROOM / CORRIDOR"),
                            ("; ", u"LL MCCS ROOM; CORRIDOR")):
    same("%-4r" % separator, value_for(circuit[:2], separator=separator)[0],
         expected)

print()
print("=== 9. What happens to each circuit")
circuits = [
    System(id=10, number="R5", panel="PDB-B2-B02-01", param="",
           devices=[device(1, "a", MCCS), device(2, "b", CORRIDOR)]),
    System(id=11, number="R6", panel="PDB-B2-B02-01",
           param=u"LL MCCS ROOM",
           devices=[device(3, "c", MCCS)]),
    System(id=12, number="R7", panel="PDB-B2-B02-01",
           param=u"typed by hand",
           devices=[device(4, "d", MCCS)]),
    System(id=13, number="R8", panel="PDB-B2-B02-01", param="", devices=[]),
    System(id=14, number="R9", panel="PDB-B2-B02-01", param="",
           devices=[device(5, "e", None)]),
    System(id=15, number="Y9", panel="PDB-B2-B02-02", param=None,
           devices=[device(6, "f", CORRIDOR)]),
]
changes = ns["build_changes"](circuits, Finder(), options(overwrite=False))
status = dict((c["number"], c["status"]) for c in changes)
same("empty parameter is filled in", status["R5"], ns["STATUS_WRITE"])
same("a value that is already right is left", status["R6"], ns["STATUS_SAME"])
same("a hand-typed value is kept unless overwrite is on", status["R7"],
     ns["STATUS_KEEP"])
same("a circuit with nothing on it is reported", status["R8"],
     ns["STATUS_NO_DEVICES"])
same("a circuit whose devices have no room is reported", status["R9"],
     ns["STATUS_NO_ROOMS"])
same("a circuit without the parameter is reported", status["Y9"],
     ns["STATUS_NO_PARAM"])
same("only the writable one is ticked",
     [c["number"] for c in changes if c["include"]], ["R5"])
same("the label reads like the tag on the drawing", changes[0]["label"],
     u"R5 / PDB-B2-B02-01")

overwritten = ns["build_changes"](circuits, Finder(), options(overwrite=True))
same("with overwrite on, the hand-typed value is replaced",
     dict((c["number"], c["status"]) for c in overwritten)["R7"],
     ns["STATUS_WRITE"])

print()
print("=== 10. Only power circuits, unless asked for all")
mixed = [System(id=1, number="R1", panel="P", kind="PowerCircuit"),
         System(id=2, number="D1", panel="P", kind="Data")]
same("a data circuit is not a power circuit",
     [ns["is_power_circuit"](s) for s in mixed], [True, False])

print()
if failures:
    print("%d check(s) FAILED: %s" % (len(failures), failures))
    sys.exit(1)
print("All circuit room-name checks passed.")
