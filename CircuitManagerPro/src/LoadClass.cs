using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;

namespace CircuitManagerPro
{
    /// <summary>One load (or spare circuit / feeder) in a load classification check.</summary>
    internal class LcRow
    {
        public long Id;
        public string Ckt;
        public string Kind;                  // "load" | "spare" | "feeder"
        public string Label;
        public List<string> Eff = new List<string>();       // keys, see LoadClass.KeyId
        public List<string> How = new List<string>();
        public List<string> Problems = new List<string>();
        public List<string> Drive;            // parameter names linked to the connectors; null = unknown
    }

    internal class LcResult
    {
        public string Panel;
        public List<LcRow> Rows = new List<LcRow>();
        public List<string> Order = new List<string>();
        public Dictionary<string, List<LcRow>> Groups = new Dictionary<string, List<LcRow>>();
        public Dictionary<string, string> Labels = new Dictionary<string, string>();
        public Dictionary<long, int> Usage = new Dictionary<long, int>();
        public Dictionary<long, ElectricalLoadClassification> Lcs;
        public Dictionary<long, List<long>> Dups;
        public List<string> Verdict = new List<string>();
        public bool? Ok;
    }

    /// <summary>
    /// Load classification check and editing.
    ///
    /// Why a panel can report several load classifications although every
    /// fixture "shows the same one":
    ///   a) two or more load classification ITEMS with the same (or nearly the
    ///      same) name - Properties shows only the name, Revit counts each item;
    ///   b) the edited parameter is not the one linked to the family's
    ///      electrical connector (fixed value in the family, other parameter,
    ///      or a text parameter);
    ///   c) a family with several power connectors with different values;
    ///   d) a downstream panel / transformer with other loads;
    ///   e) a spare circuit with another classification.
    /// </summary>
    internal static class LoadClass
    {
        public const string KeyNone = "none";
        public const string KeyInfo = "__info__";
        public static string KeyId(long v) => "id:" + v;
        public static bool IsIdKey(string k) => k.StartsWith("id:", StringComparison.Ordinal);
        public static long IdOf(string k) => long.Parse(k.Substring(3));

        // ---------------------------------------------------------------- names
        /// <summary>Name in quotes; spaces at either end shown as a dot,
        /// non-ASCII characters as \uXXXX.</summary>
        public static string Visible(string name)
        {
            if (name == null) return "<none>";
            var sb = new StringBuilder();
            foreach (char ch in name)
            {
                if (ch > 126 || ch < 32) sb.AppendFormat("\\u{0:X4}", (int)ch);
                else sb.Append(ch);
            }
            string s = sb.ToString();
            string core = s.Trim(' ');
            int lead = s.Length - s.TrimStart(' ').Length;
            int trail = s.Length - s.TrimEnd(' ').Length;
            string outp = "'" + new string('·', lead) + core + new string('·', Math.Max(0, trail)) + "'";
            if (core.Contains("  ")) outp += " [double space]";
            return outp;
        }

        public static string Norm(string name)
            => string.Join(" ", (name ?? "").Replace(' ', ' ')
                .Split((char[])null, StringSplitOptions.RemoveEmptyEntries)).ToLowerInvariant();

        public static string Clean(string name)
            => string.Join(" ", (name ?? "").Replace(' ', ' ')
                .Split((char[])null, StringSplitOptions.RemoveEmptyEntries));

        public static Dictionary<long, ElectricalLoadClassification> All(Document doc)
            => new FilteredElementCollector(doc)
                .OfClass(typeof(ElectricalLoadClassification))
                .Cast<ElectricalLoadClassification>()
                .ToDictionary(l => Rvt.Val(l.Id));

        public static IEnumerable<KeyValuePair<long, ElectricalLoadClassification>> Sorted(
            Dictionary<long, ElectricalLoadClassification> lcs)
            => lcs.OrderBy(kv => Norm(kv.Value.Name)).ThenBy(kv => kv.Key);

        /// <summary>{id: other ids whose name is the same after trimming / ignoring case}</summary>
        public static Dictionary<long, List<long>> Duplicates(Dictionary<long, ElectricalLoadClassification> lcs)
        {
            var outp = new Dictionary<long, List<long>>();
            foreach (var grp in lcs.GroupBy(kv => Norm(kv.Value.Name)))
            {
                var ids = grp.Select(kv => kv.Key).ToList();
                if (ids.Count < 2) continue;
                foreach (long v in ids) outp[v] = ids.Where(x => x != v).ToList();
            }
            return outp;
        }

        public static long? NameConflict(string name, Dictionary<long, ElectricalLoadClassification> lcs, long? exclude = null)
        {
            string n = Norm(name);
            foreach (var kv in lcs)
                if (kv.Key != exclude && Norm(kv.Value.Name) == n) return kv.Key;
            return null;
        }

        public static List<KeyValuePair<long, string>> DemandFactors(Document doc)
            => new FilteredElementCollector(doc)
                .OfClass(typeof(ElectricalDemandFactorDefinition))
                .Select(d => new KeyValuePair<long, string>(Rvt.Val(d.Id), d.Name))
                .OrderBy(kv => kv.Value, StringComparer.OrdinalIgnoreCase)
                .ToList();

        // ----------------------------------------------------------- parameters
        private static IEnumerable<KeyValuePair<string, Element>> Owners(Element el)
        {
            yield return new KeyValuePair<string, Element>("instance", el);
            if (el is FamilyInstance fi && fi.Symbol != null)
                yield return new KeyValuePair<string, Element>("type", fi.Symbol);
        }

        public static bool IsLcParam(Parameter p, Dictionary<long, ElectricalLoadClassification> lcs)
        {
            if (p == null || p.StorageType != StorageType.ElementId) return false;
            try
            {
                if (p.Definition.GetDataType() == SpecTypeId.Reference.LoadClassification) return true;
            }
            catch (Exception) { }
            try { return lcs.ContainsKey(Rvt.Val(p.AsElementId())); }
            catch (Exception) { return false; }
        }

        /// <summary>Every load classification parameter on the element ("instance")
        /// and on its type ("type").</summary>
        public static List<KeyValuePair<string, Parameter>> LcParams(Element el, Dictionary<long, ElectricalLoadClassification> lcs)
        {
            var found = new List<KeyValuePair<string, Parameter>>();
            foreach (var owner in Owners(el))
                foreach (Parameter p in owner.Value.Parameters)
                    if (IsLcParam(p, lcs)) found.Add(new KeyValuePair<string, Parameter>(owner.Key, p));
            return found;
        }

        /// <summary>Text parameters whose name looks like "load classification".</summary>
        private static List<string[]> LcTextParams(Element el)
        {
            var found = new List<string[]>();
            foreach (var owner in Owners(el))
                foreach (Parameter p in owner.Value.Parameters)
                {
                    if (p.StorageType != StorageType.String) continue;
                    string n = p.Definition.Name.ToLowerInvariant();
                    if (n.Contains("load") && n.Contains("class"))
                        found.Add(new[] { owner.Key, p.Definition.Name, p.AsString() ?? "" });
                }
            return found;
        }

        /// <summary>Reads the element's family file: (classification name, linked
        /// family parameter name or null) for every power connector. null = unreadable.</summary>
        public static List<KeyValuePair<string, string>> FamilyConnectors(
            Document doc, FamilyInstance fi, Dictionary<long, List<KeyValuePair<string, string>>> cache)
        {
            Family fam = fi.Symbol?.Family;
            if (fam == null) return null;
            long key = Rvt.Val(fam.Id);
            if (cache.TryGetValue(key, out var hit)) return hit;
            if (fam.IsInPlace || !fam.IsEditable)
            {
                cache[key] = null;
                return null;
            }

            List<KeyValuePair<string, string>> result = null;
            var alreadyOpen = doc.Application.Documents.Cast<Document>().ToList();
            Document fdoc = null;
            try
            {
                fdoc = doc.EditFamily(fam);
                result = new List<KeyValuePair<string, string>>();
                foreach (ConnectorElement ce in new FilteredElementCollector(fdoc).OfClass(typeof(ConnectorElement)))
                {
                    if (ce.Domain != Domain.DomainElectrical) continue;
                    Parameter p = ce.get_Parameter(BuiltInParameter.RBS_ELEC_LOAD_CLASSIFICATION);
                    if (p == null) continue;          // data / comms connector
                    string name = fdoc.GetElement(p.AsElementId())?.Name;
                    string assoc = null;
                    try { assoc = fdoc.FamilyManager.GetAssociatedFamilyParameter(p)?.Definition.Name; }
                    catch (Exception) { }
                    result.Add(new KeyValuePair<string, string>(name, assoc));
                }
            }
            catch (Exception)
            {
                result = null;
            }
            finally
            {
                if (fdoc != null && !alreadyOpen.Any(d => d.Equals(fdoc)))
                {
                    try { fdoc.Close(false); } catch (Exception) { }
                }
            }
            cache[key] = result;
            return result;
        }

        /// <summary>Key for a classification name set inside a family.</summary>
        private static string ResolveName(string name, Dictionary<long, ElectricalLoadClassification> lcs)
        {
            if (name == null) return KeyNone;
            var exact = lcs.Where(kv => kv.Value.Name == name).Select(kv => kv.Key).ToList();
            return exact.Count == 1 ? KeyId(exact[0]) : "name:" + name;
        }

        /// <summary>{classification id: (owner, parameter)} for every load
        /// classification parameter in the project: loads, their types, circuits.</summary>
        public static Dictionary<long, List<KeyValuePair<Element, Parameter>>> ProjectRefs(
            Document doc, Dictionary<long, ElectricalLoadClassification> lcs)
        {
            var refs = new Dictionary<long, List<KeyValuePair<Element, Parameter>>>();
            void Add(Element owner, Parameter p)
            {
                long v = Rvt.Val(p.AsElementId());
                if (!refs.TryGetValue(v, out var list)) refs[v] = list = new List<KeyValuePair<Element, Parameter>>();
                list.Add(new KeyValuePair<Element, Parameter>(owner, p));
            }

            var filter = new ElementMulticategoryFilter(Rvt.CircuitableBics.ToList());
            var namesByType = new Dictionary<long, List<string>>();
            foreach (FamilyInstance el in new FilteredElementCollector(doc).WherePasses(filter)
                         .WhereElementIsNotElementType().OfType<FamilyInstance>())
            {
                FamilySymbol sym = el.Symbol;
                if (sym == null) continue;
                long sk = Rvt.Val(sym.Id);
                if (!namesByType.TryGetValue(sk, out var names))
                {
                    names = new List<string>();
                    foreach (Parameter p in el.Parameters)
                        if (IsLcParam(p, lcs)) names.Add(p.Definition.Name);
                    namesByType[sk] = names;
                    foreach (Parameter p in sym.Parameters)
                        if (IsLcParam(p, lcs)) Add(sym, p);
                }
                foreach (string n in names)
                {
                    Parameter p = el.LookupParameter(n);
                    if (p != null && p.StorageType == StorageType.ElementId) Add(el, p);
                }
            }
            foreach (ElectricalSystem c in new FilteredElementCollector(doc).OfClass(typeof(ElectricalSystem)))
                foreach (Parameter p in c.Parameters)
                    if (IsLcParam(p, lcs)) Add(c, p);
            return refs;
        }

        // ------------------------------------------------------------- analysis
        private static LcRow Analyse(Document doc, Element el, string ckt,
            Dictionary<long, ElectricalLoadClassification> lcs, bool deep, bool spare,
            Dictionary<long, List<KeyValuePair<string, string>>> famCache)
        {
            var row = new LcRow
            {
                Id = Rvt.Val(el.Id), Ckt = ckt, Kind = spare ? "spare" : "load",
                Label = spare ? "Spare / space circuit" : Rvt.ElementLabel(el),
            };
            // (where, name, key)
            var pvals = new List<string[]>();
            foreach (var wp in LcParams(el, lcs))
            {
                long v = Rvt.Val(wp.Value.AsElementId());
                pvals.Add(new[] { wp.Key, wp.Value.Definition.Name, lcs.ContainsKey(v) ? KeyId(v) : KeyNone });
            }

            List<KeyValuePair<string, string>> fam = null;
            if (deep && !spare && el is FamilyInstance fi) fam = FamilyConnectors(doc, fi, famCache);

            if (fam != null && fam.Count > 0)
            {
                row.Drive = fam.Where(c => c.Value != null).Select(c => c.Value).ToList();
                foreach (var con in fam)
                {
                    if (con.Value != null)
                    {
                        string[] match = pvals.FirstOrDefault(pv => pv[1] == con.Value);
                        if (match != null)
                        {
                            row.Eff.Add(match[2]);
                            row.How.Add($"connector uses {match[0]} parameter '{match[1]}'");
                            if (match[2] == KeyNone) row.Problems.Add($"parameter '{match[1]}' is empty");
                        }
                        else
                        {
                            row.Eff.Add(KeyNone);
                            row.How.Add($"connector uses parameter '{con.Value}'");
                            row.Problems.Add($"connector is linked to '{con.Value}', which was not found on the element");
                        }
                    }
                    else
                    {
                        row.Eff.Add(ResolveName(con.Key, lcs));
                        row.How.Add("fixed inside the family: " + Visible(con.Key));
                    }
                }
                var notDriving = pvals.Where(pv => !row.Drive.Contains(pv[1])).Select(pv => "'" + pv[1] + "'").ToList();
                if (notDriving.Count > 0)
                    row.Problems.Add($"parameter {string.Join(", ", notDriving)} is NOT linked to the electrical connector - "
                                     + "changing it does nothing. In the family, link the connector's Load Classification to it.");
                if (fam.Count > 1 && row.Eff.Distinct().Count() > 1)
                    row.Problems.Add($"family has {fam.Count} power connectors with different load classifications");
            }
            else if (pvals.Count > 0)
            {
                foreach (string[] pv in pvals)
                {
                    row.Eff.Add(pv[2]);
                    row.How.Add($"{pv[0]} parameter '{pv[1]}'");
                    if (pv[2] == KeyNone) row.Problems.Add($"parameter '{pv[1]}' is empty");
                }
                if (pvals.Select(pv => pv[2]).Distinct().Count() > 1)
                    row.Problems.Add($"{pvals.Count} load classification parameters with different values - only one drives "
                                     + "the connector (tick 'Read the families' to see which)");
            }
            else if (spare)
            {
                row.How.Add("no load classification");
            }
            else
            {
                row.Eff.Add(KeyNone);
                row.How.Add(deep ? "family could not be read (in-place or protected family)"
                                 : "set inside the family - tick 'Read the families' to see it");
            }

            if (pvals.Count == 0)
                foreach (string[] t in LcTextParams(el))
                    row.Problems.Add($"{t[0]} parameter '{t[1]}' = '{t[2]}' is only TEXT - it is not the connector's load classification");
            return row;
        }

        public static LcResult Run(Document doc, FamilyInstance panel, bool downstream, bool deep,
            Dictionary<long, List<KeyValuePair<string, string>>> famCache)
        {
            var res = new LcResult { Panel = Rvt.PanelName(panel), Lcs = All(doc) };
            res.Dups = Duplicates(res.Lcs);
            var lcs = res.Lcs;

            var byPanel = new Dictionary<long, List<ElectricalSystem>>();
            foreach (ElectricalSystem c in new FilteredElementCollector(doc).OfClass(typeof(ElectricalSystem)))
            {
                FamilyInstance be = null;
                try { be = c.BaseEquipment; } catch (Exception) { }
                if (be == null) continue;
                long k = Rvt.Val(be.Id);
                if (!byPanel.TryGetValue(k, out var l)) byPanel[k] = l = new List<ElectricalSystem>();
                l.Add(c);
            }

            var circuitValues = new Dictionary<string, int>();
            var visited = new HashSet<long>();

            void Walk(Element equip, string path)
            {
                if (!visited.Add(Rvt.Val(equip.Id))) return;
                if (!byPanel.TryGetValue(Rvt.Val(equip.Id), out var circuits)) return;
                foreach (ElectricalSystem c in circuits.OrderBy(Rvt.CircuitNo))
                {
                    if (c.SystemType != ElectricalSystemType.PowerCircuit) continue;
                    string ckt = $"{path} / Ckt {(string.IsNullOrEmpty(c.CircuitNumber) ? "-" : c.CircuitNumber)}";
                    string val = "";
                    try { val = c.LoadClassifications ?? ""; } catch (Exception) { }
                    if (val.Length > 0) circuitValues[val] = circuitValues.TryGetValue(val, out int n) ? n + 1 : 1;

                    var elems = c.Elements.Cast<Element>().ToList();
                    if (elems.Count == 0)
                    {
                        res.Rows.Add(Analyse(doc, c, ckt, lcs, deep, true, famCache));
                        continue;
                    }
                    foreach (Element e in elems)
                    {
                        if (Rvt.IsElectricalEquipment(e) && byPanel.ContainsKey(Rvt.Val(e.Id)))
                        {
                            if (downstream)
                                Walk(e, path + " > " + Rvt.PanelName(e));
                            else
                            {
                                string sub = Rvt.PanelName(e);
                                var row = new LcRow
                                {
                                    Id = Rvt.Val(e.Id), Ckt = ckt, Kind = "feeder", Label = Rvt.ElementLabel(e),
                                };
                                row.How.Add($"feeds panel '{sub}'");
                                row.Problems.Add($"Revit adds the loads of '{sub}' (and their load classifications) to this "
                                                 + "panel - tick 'Include downstream panels' to check them");
                                res.Rows.Add(row);
                            }
                            continue;
                        }
                        res.Rows.Add(Analyse(doc, e, ckt, lcs, deep, false, famCache));
                    }
                }
            }
            Walk(panel, res.Panel);

            var order = new List<string>();
            foreach (LcRow r in res.Rows)
            {
                var keys = (r.Eff.Count > 0 ? r.Eff : new List<string> { KeyInfo }).Distinct();
                foreach (string k in keys)
                {
                    if (!res.Groups.TryGetValue(k, out var list))
                    {
                        res.Groups[k] = list = new List<LcRow>();
                        order.Add(k);
                    }
                    list.Add(r);
                    if (IsIdKey(k))
                    {
                        long id = IdOf(k);
                        res.Usage[id] = res.Usage.TryGetValue(id, out int u) ? u + 1 : 1;
                    }
                }
            }
            var counted = order.Where(k => k != KeyInfo).OrderByDescending(k => res.Groups[k].Count).ToList();
            res.Order.AddRange(counted);
            if (res.Groups.ContainsKey(KeyInfo)) res.Order.Add(KeyInfo);
            foreach (string k in res.Order) res.Labels[k] = KeyLabel(k, lcs, res.Dups);

            int nLoads = res.Rows.Count(r => r.Kind == "load");
            int nProb = res.Rows.Count(r => r.Problems.Count > 0);
            if (res.Rows.Count == 0)
            {
                res.Verdict.Add($"Panel '{res.Panel}' has no circuits with loads.");
                res.Ok = null;
            }
            else if (counted.Count == 1 && IsIdKey(counted[0]) && nProb == 0)
            {
                res.Ok = true;
                res.Verdict.Add($"OK - all {nLoads} load(s) on '{res.Panel}' use ONE load classification: {res.Labels[counted[0]]}");
            }
            else
            {
                res.Ok = false;
                res.Verdict.Add($"PROBLEM - {counted.Count} different load classification(s) feed '{res.Panel}' ({nLoads} loads):");
                foreach (string k in counted)
                    res.Verdict.Add($"   - {res.Labels[k]}  ->  {res.Groups[k].Count} element(s)");
                bool same = counted.Any(k => IsIdKey(k) && res.Dups.ContainsKey(IdOf(k))
                                             && res.Dups[IdOf(k)].Any(o => counted.Contains(KeyId(o))));
                if (same)
                    res.Verdict.Add("CAUSE: the project has load classifications with the SAME NAME. They look identical in "
                                    + "Properties, but they are different items, so Revit lists them separately. Fix: in the "
                                    + "project list on the right select the spare one(s), then New / merge > Merge - or select "
                                    + "all rows and use Fix loads.");
                else if (counted.Any(k => IsIdKey(k) && res.Dups.ContainsKey(IdOf(k))))
                    res.Verdict.Add("Note: a classification used here has a twin with the same name in the project.");
                if (nProb > 0) res.Verdict.Add($"{nProb} row(s) have warnings (red) - read them in the list.");
                if (!deep)
                    res.Verdict.Add("Tip: tick 'Read the families' - the value you see in Properties is not always the one "
                                    + "the electrical connector uses.");
            }
            if (circuitValues.Count > 0)
                res.Verdict.Add("Revit's circuit 'Load Classification' values: " + string.Join(";  ",
                    circuitValues.OrderByDescending(kv => kv.Value).Take(8).Select(kv => $"{Visible(kv.Key)} ({kv.Value} ckt)")));
            return res;
        }

        private static string KeyLabel(string k, Dictionary<long, ElectricalLoadClassification> lcs,
            Dictionary<long, List<long>> dups)
        {
            if (k == KeyInfo) return "Not counted as a load (spare circuits / feeders)";
            if (k == KeyNone) return "UNKNOWN or EMPTY load classification";
            if (IsIdKey(k))
            {
                long id = IdOf(k);
                string txt = $"{Visible(lcs[id].Name)}   (id {id})";
                if (dups.ContainsKey(id)) txt += "   !! SAME NAME AS id " + string.Join(", ", dups[id]);
                return txt;
            }
            string name = k.Substring(5);
            int n = lcs.Count(kv => kv.Value.Name == name);
            return n > 1
                ? $"{Visible(name)} set inside families - {n} project classifications have this name"
                : $"{Visible(name)} set inside families - no project classification has exactly this name";
        }
    }
}
