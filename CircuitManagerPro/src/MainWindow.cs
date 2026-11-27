using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Markup;
using System.Windows.Media;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.DB.Events;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Selection;
using Border = System.Windows.Controls.Border;
using ComboBox = System.Windows.Controls.ComboBox;
using TextBox = System.Windows.Controls.TextBox;

namespace CircuitManagerPro
{
    /// <summary>
    /// The modeless Circuit Manager Pro panel. UI events are queued to the
    /// ApiQueue and run inside Revit's API context (Run), one at a time.
    /// </summary>
    internal class MainWindow
    {
        public readonly Window Win;

        private readonly Document _doc;
        private readonly string _docTitle;
        private UIDocument _uidoc;
        private readonly ApiQueue _queue;
        private readonly EventHandler<DocumentChangedEventArgs> _docChanged;
        private bool _busy, _closed, _dirty;

        private readonly List<string> _log = new List<string>();
        private readonly List<long> _fixtures = new List<long>();
        private readonly Dictionary<long, List<KeyValuePair<string, string>>> _famCache =
            new Dictionary<long, List<KeyValuePair<string, string>>>();
        private LcResult _lc;
        private Dictionary<string, List<long>> _lcGroupIds = new Dictionary<string, List<long>>();
        private Dictionary<long, int> _lcUse = new Dictionary<long, int>();
        private static readonly BrushConverter Brush = new BrushConverter();

        // controls
        private readonly TabControl tabs;
        private readonly TextBlock txtDoc, lblFixtures, lblCircuits, lblSelCount, lblEditElems, txtLcVerdict;
        private readonly ListBox lstFixtures, lstCircuits, lstEditElems, lstLcRows, lstLcProject;
        private readonly ComboBox cmbCreatePanel, cmbFilterPanel, cmbMovePanel, cmbEditCircuit,
            cmbLcPanel, cmbLcTarget, cmbLcDemand, cmbLcMerge;
        private readonly System.Windows.Controls.CheckBox chkSkip, chkLcDown, chkLcDeep;
        private readonly TextBox txtSearch, txtLog, txtLcName, txtLcAbbr, txtLcNewName, txtLcNewAbbr;
        private readonly Border bdLcVerdict;

        public MainWindow(UIApplication uiapp, ApiQueue queue)
        {
            _queue = queue;
            _uidoc = uiapp.ActiveUIDocument;
            _doc = _uidoc.Document;
            _docTitle = _doc.Title;

            using (Stream s = typeof(MainWindow).Assembly.GetManifestResourceStream("CircuitManagerPro.MainWindow.xaml"))
                Win = (Window)XamlReader.Load(s);

            tabs = F<TabControl>("tabs");
            txtDoc = F<TextBlock>("txtDoc"); lblFixtures = F<TextBlock>("lblFixtures");
            lblCircuits = F<TextBlock>("lblCircuits"); lblSelCount = F<TextBlock>("lblSelCount");
            lblEditElems = F<TextBlock>("lblEditElems"); txtLcVerdict = F<TextBlock>("txtLcVerdict");
            lstFixtures = F<ListBox>("lstFixtures"); lstCircuits = F<ListBox>("lstCircuits");
            lstEditElems = F<ListBox>("lstEditElems"); lstLcRows = F<ListBox>("lstLcRows");
            lstLcProject = F<ListBox>("lstLcProject");
            cmbCreatePanel = F<ComboBox>("cmbCreatePanel"); cmbFilterPanel = F<ComboBox>("cmbFilterPanel");
            cmbMovePanel = F<ComboBox>("cmbMovePanel"); cmbEditCircuit = F<ComboBox>("cmbEditCircuit");
            cmbLcPanel = F<ComboBox>("cmbLcPanel"); cmbLcTarget = F<ComboBox>("cmbLcTarget");
            cmbLcDemand = F<ComboBox>("cmbLcDemand"); cmbLcMerge = F<ComboBox>("cmbLcMerge");
            chkSkip = F<System.Windows.Controls.CheckBox>("chkSkip");
            chkLcDown = F<System.Windows.Controls.CheckBox>("chkLcDown");
            chkLcDeep = F<System.Windows.Controls.CheckBox>("chkLcDeep");
            txtSearch = F<TextBox>("txtSearch"); txtLog = F<TextBox>("txtLog");
            txtLcName = F<TextBox>("txtLcName"); txtLcAbbr = F<TextBox>("txtLcAbbr");
            txtLcNewName = F<TextBox>("txtLcNewName"); txtLcNewAbbr = F<TextBox>("txtLcNewAbbr");
            bdLcVerdict = F<Border>("bdLcVerdict");

            txtDoc.Text = $"Project: {_docTitle}   -   keep working in Revit while this panel is open   -   each action is one Undo step";

            FillPanelCombos();
            FillFixtures();
            FillCircuits(null);
            FillEditCombo();
            var lcs = LoadClass.All(_doc);
            FillLcDemand();
            FillLcProject(lcs, null);
            FillLcTarget(lcs, null);
            RefreshLog();

            // buttons
            Click("btnPickFix", OnPickFixtures); Click("btnRemFix", OnRemoveFix);
            Click("btnClearFix", OnClearFix); Click("btnCreate", OnCreate);
            Click("btnSelAll", () => { lstCircuits.SelectAll(); UpdateSelCount(); });
            Click("btnSelNone", () => { lstCircuits.UnselectAll(); UpdateSelCount(); });
            Click("btnPickCkt", OnPickCircuits); Click("btnRefresh", OnRefresh);
            Click("btnMove", OnMove); Click("btnDisconnect", OnDisconnect);
            Click("btnEditSel", OnEditSelected); Click("btnDelete", OnDelete);
            Click("btnAddElems", OnAddElems); Click("btnRemElems", OnRemoveElems);
            Click("btnLcRun", () => LcRun(true)); Click("btnLcFixSel", () => LcApply(LcSelectedIds()));
            Click("btnLcFixAll", () => { if (_lc != null) LcApply(_lc.Rows.Where(r => r.Kind != "feeder").Select(r => r.Id).ToList()); });
            Click("btnLcSelect", OnLcSelect);
            Click("btnLcSave", OnLcSave); Click("btnLcClean", OnLcClean);
            Click("btnLcDelete", OnLcDelete); Click("btnLcNew", OnLcNew); Click("btnLcMerge", OnLcMerge);
            F<System.Windows.Controls.Button>("btnClose").Click += (s, e) => Win.Close();

            // lists
            cmbFilterPanel.SelectionChanged += (s, e) => Post(() => FillCircuits(null));
            txtSearch.TextChanged += (s, e) => Post(() => FillCircuits(null));
            lstCircuits.SelectionChanged += (s, e) => { if (!_busy) UpdateSelCount(); };
            cmbEditCircuit.SelectionChanged += (s, e) => Post(FillEditElems);
            lstLcProject.SelectionChanged += (s, e) => Post(OnLcProjectSelection);

            // modeless plumbing
            _docChanged = OnDocChanged;
            uiapp.Application.DocumentChanged += _docChanged;
            Win.Activated += OnActivated;
            Win.Closed += OnClosed;
            try { new WindowInteropHelper(Win).Owner = uiapp.MainWindowHandle; }
            catch (Exception) { }
        }

        private T F<T>(string name) where T : class
            => Win.FindName(name) as T ?? throw new InvalidOperationException("Missing control " + name);

        public bool IsFor(Document doc) => _doc.IsValidObject && _doc.Equals(doc);

        public void BringToFront()
        {
            if (Win.WindowState == WindowState.Minimized) Win.WindowState = WindowState.Normal;
            Win.Show();
            Win.Activate();
        }

        // =============================================================== plumbing
        private void Click(string name, Action action)
            => F<System.Windows.Controls.Button>(name).Click += (s, e) => Post(action);

        /// <summary>Queue an action to run in Revit's API context.</summary>
        private void Post(Action action)
        {
            if (_busy || _closed) return;       // events caused by our own list updates
            _queue.Post(app => Run(action, app));
        }

        private void Run(Action action, UIApplication app)
        {
            if (_closed || !DocOk(app)) return;
            _busy = true;
            try { action(); }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException) { }
            catch (Exception ex)
            {
                Log("Error: " + ex.Message);
                RefreshLog();
            }
            finally { _busy = false; }
        }

        private bool DocOk(UIApplication app)
        {
            try
            {
                if (!_doc.IsValidObject)
                {
                    Win.Close();
                    return false;
                }
                UIDocument active = app.ActiveUIDocument;
                if (active == null || !active.Document.Equals(_doc))
                {
                    Log($"Circuit Manager Pro works on '{_docTitle}' - switch back to that project and try again.");
                    RefreshLog();
                    return false;
                }
                _uidoc = active;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private void OnDocChanged(object sender, DocumentChangedEventArgs e)
        {
            try
            {
                if (!e.GetDocument().Equals(_doc)) return;
                _dirty = true;
                // a reloaded family must be read again by the load classification check
                foreach (ElementId id in e.GetModifiedElementIds().Concat(e.GetAddedElementIds()))
                    _famCache.Remove(Rvt.Val(id));
            }
            catch (Exception) { }
        }

        private void OnActivated(object sender, EventArgs e)
        {
            if (!_dirty || _busy || _closed) return;
            _dirty = false;
            _queue.Post(app => Run(DocRefresh, app));
        }

        /// <summary>Back in the panel after changing the model: refresh stale lists.</summary>
        private void DocRefresh()
        {
            FillPanelCombos();
            RefreshAll();
            var lcs = LoadClass.All(_doc);
            FillLcProject(lcs, _lc?.Usage);
            FillLcTarget(lcs, null);
        }

        private void OnClosed(object sender, EventArgs e)
        {
            _closed = true;
            if (App.Window == this) App.Window = null;
            EventHandler<DocumentChangedEventArgs> handler = _docChanged;
            _queue.Post(app =>
            {
                try { app.Application.DocumentChanged -= handler; } catch (Exception) { }
            });
        }

        public void Log(string msg) => _log.Add(msg);

        private void RefreshLog()
        {
            txtLog.Text = _log.Count == 0
                ? "Ready. Pick fixtures on the first tab, or manage existing circuits on the others."
                : string.Join("\n", _log.Skip(Math.Max(0, _log.Count - 200)));
            txtLog.ScrollToEnd();
        }

        // ================================================================ helpers
        private static ComboBoxItem ComboItem(string text, object tag) => new ComboBoxItem { Content = text, Tag = tag };
        private static ListBoxItem ListItem(string text, object tag) => new ListBoxItem { Content = text, Tag = tag };

        private static void SelectTag(ComboBox combo, object tag)
        {
            for (int i = 0; i < combo.Items.Count; i++)
            {
                if (Equals(((ComboBoxItem)combo.Items[i]).Tag, tag))
                {
                    combo.SelectedIndex = i;
                    return;
                }
            }
            if (combo.Items.Count > 0) combo.SelectedIndex = 0;
        }

        private static object Tag(ComboBox combo) => (combo.SelectedItem as ComboBoxItem)?.Tag;
        private static List<object> SelectedTags(ListBox list) => list.SelectedItems.Cast<ListBoxItem>().Select(i => i.Tag).ToList();
        private static System.Windows.Media.Brush Color(string hex) => (System.Windows.Media.Brush)Brush.ConvertFromString(hex);

        private List<Element> Pick(string prompt)
        {
            try
            {
                return _uidoc.Selection.PickObjects(ObjectType.Element, prompt)
                    .Select(r => _doc.GetElement(r.ElementId)).Where(e => e != null).ToList();
            }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException)
            {
                return new List<Element>();
            }
        }

        /// <summary>Hide the panel while the user picks in the model.</summary>
        private void WithPanelHidden(Action pick)
        {
            Win.Hide();
            try { pick(); }
            finally
            {
                Win.Show();
                Win.Activate();
            }
        }

        private MessageBoxResult Ask(string text)
            => MessageBox.Show(Win, text, "Circuit Manager Pro", MessageBoxButton.YesNo, MessageBoxImage.Question);

        // ============================================================= fill lists
        private void FillPanelCombos()
        {
            object cp = Tag(cmbCreatePanel), fp = Tag(cmbFilterPanel), mp = Tag(cmbMovePanel), lp = Tag(cmbLcPanel);
            foreach (ComboBox c in new[] { cmbCreatePanel, cmbFilterPanel, cmbMovePanel, cmbLcPanel }) c.Items.Clear();
            cmbCreatePanel.Items.Add(ComboItem("(no panel - leave unassigned)", null));
            cmbFilterPanel.Items.Add(ComboItem("(all circuits)", "__all__"));
            cmbFilterPanel.Items.Add(ComboItem("(circuits with no panel)", "__none__"));
            foreach (FamilyInstance p in Rvt.AllPanels(_doc))
            {
                long id = Rvt.Val(p.Id);
                string name = Rvt.PanelName(p);
                cmbCreatePanel.Items.Add(ComboItem(name, id));
                cmbFilterPanel.Items.Add(ComboItem(name, id));
                cmbMovePanel.Items.Add(ComboItem(name, id));
                cmbLcPanel.Items.Add(ComboItem(name, id));
            }
            SelectTag(cmbCreatePanel, cp);
            SelectTag(cmbFilterPanel, fp ?? "__all__");
            SelectTag(cmbMovePanel, mp);
            SelectTag(cmbLcPanel, lp);
        }

        private void FillFixtures()
        {
            lstFixtures.Items.Clear();
            _fixtures.RemoveAll(v => _doc.GetElement(Rvt.Id(v)) == null);
            foreach (long v in _fixtures)
            {
                Element el = _doc.GetElement(Rvt.Id(v));
                string txt = Rvt.ElementLabel(el);
                var ps = Rvt.PowerSystems(el);
                if (ps.Count > 0)
                    txt += "   * on circuit " + string.Join(", ", ps.Select(s => (s.PanelName ?? "?") + "/" + (s.CircuitNumber ?? "-")));
                lstFixtures.Items.Add(ListItem(txt, v));
            }
            lblFixtures.Text = $"Fixtures to circuit ({_fixtures.Count})";
        }

        private void FillCircuits(HashSet<long> keep)
        {
            if (keep == null) keep = new HashSet<long>(SelectedTags(lstCircuits).OfType<long>());
            object filt = Tag(cmbFilterPanel);
            string search = (txtSearch.Text ?? "").Trim().ToLowerInvariant();
            lstCircuits.Items.Clear();
            var all = Rvt.AllCircuits(_doc);
            int shown = 0;
            foreach (ElectricalSystem c in all)
            {
                FamilyInstance be = c.BaseEquipment;
                if (Equals(filt, "__none__") && be != null) continue;
                if (filt is long pid && (be == null || Rvt.Val(be.Id) != pid)) continue;
                string label = Rvt.CircuitLabel(c);
                if (search.Length > 0 && !label.ToLowerInvariant().Contains(search)) continue;
                long cid = Rvt.Val(c.Id);
                var it = ListItem(label, cid);
                lstCircuits.Items.Add(it);
                if (keep.Contains(cid)) it.IsSelected = true;
                shown++;
            }
            lblCircuits.Text = $"Circuits  ({shown} shown of {all.Count})";
            UpdateSelCount();
        }

        private void UpdateSelCount() => lblSelCount.Text = $"{lstCircuits.SelectedItems.Count} circuit(s) selected";

        private void FillEditCombo()
        {
            object keep = Tag(cmbEditCircuit);
            cmbEditCircuit.Items.Clear();
            foreach (ElectricalSystem c in Rvt.AllCircuits(_doc))
                cmbEditCircuit.Items.Add(ComboItem(Rvt.CircuitLabel(c), Rvt.Val(c.Id)));
            SelectTag(cmbEditCircuit, keep);
            FillEditElems();
        }

        private void FillEditElems()
        {
            lstEditElems.Items.Clear();
            if (!(Rvt.Get(_doc, Tag(cmbEditCircuit)) is ElectricalSystem c))
            {
                lblEditElems.Text = "No circuit selected";
                return;
            }
            int n = 0;
            foreach (Element e in c.Elements)
            {
                lstEditElems.Items.Add(ListItem(Rvt.ElementLabel(e), Rvt.Val(e.Id)));
                n++;
            }
            lblEditElems.Text = $"{n} element(s) on this circuit";
        }

        private void RefreshAll()
        {
            FillFixtures();
            FillCircuits(null);
            FillEditCombo();
            RefreshLog();
        }

        private List<ElectricalSystem> SelectedCircuits()
            => SelectedTags(lstCircuits).Select(t => Rvt.Get(_doc, t)).OfType<ElectricalSystem>().ToList();

        // ================================================================= TAB 1
        private void OnPickFixtures()
        {
            WithPanelHidden(() =>
            {
                var picked = Pick("Pick fixtures / devices to circuit, then click Finish");
                int added = 0, ignored = 0;
                foreach (Element el in picked)
                {
                    if (!Rvt.IsCircuitable(el)) { ignored++; continue; }
                    long v = Rvt.Val(el.Id);
                    if (_fixtures.Contains(v)) continue;
                    _fixtures.Add(v);
                    added++;
                }
                Log($"Pick: {added} element(s) added to the list" + (ignored > 0 ? $", {ignored} ignored (no electrical connector)" : "") + ".");
            });
            tabs.SelectedIndex = 0;
            FillFixtures();
            RefreshLog();
        }

        private void OnRemoveFix()
        {
            var rem = new HashSet<long>(SelectedTags(lstFixtures).OfType<long>());
            _fixtures.RemoveAll(rem.Contains);
            FillFixtures();
        }

        private void OnClearFix()
        {
            _fixtures.Clear();
            FillFixtures();
        }

        private void OnCreate()
        {
            if (_fixtures.Count == 0)
            {
                Log("Create: the list is empty - pick fixtures first.");
                RefreshLog();
                return;
            }
            bool skip = chkSkip.IsChecked == true;
            var panel = Rvt.Get(_doc, Tag(cmbCreatePanel)) as FamilyInstance;
            int made = 0, skipped = 0, failed = 0, noPanel = 0;
            Rvt.InTx(_doc, "Create circuits", () =>
            {
                foreach (long v in _fixtures)
                {
                    Element el = _doc.GetElement(Rvt.Id(v));
                    if (el == null) continue;
                    if (skip && Rvt.PowerSystems(el).Count > 0) { skipped++; continue; }
                    ElectricalSystem sys;
                    try
                    {
                        sys = ElectricalSystem.Create(_doc, new List<ElementId> { el.Id }, ElectricalSystemType.PowerCircuit);
                    }
                    catch (Exception ex)
                    {
                        failed++;
                        Log($"  x {Rvt.ElementLabel(el)}: {ex.Message}");
                        continue;
                    }
                    if (sys == null)
                    {
                        failed++;
                        Log($"  x {Rvt.ElementLabel(el)}: Revit returned no circuit");
                        continue;
                    }
                    made++;
                    if (panel == null) continue;
                    try { sys.SelectPanel(panel); }
                    catch (Exception ex)
                    {
                        noPanel++;
                        Log($"  ! circuit for id {v} not assigned to {Rvt.PanelName(panel)}: {ex.Message}");
                    }
                }
            });
            string msg = $"Create: {made} circuit(s) created";
            if (panel != null) msg += " on " + Rvt.PanelName(panel);
            if (skipped > 0) msg += $", {skipped} skipped (already circuited)";
            if (failed > 0) msg += $", {failed} failed";
            if (noPanel > 0) msg += $", {noPanel} left without panel";
            Log(msg + ".");
            RefreshAll();
        }

        // ================================================================= TAB 2
        private void OnPickCircuits()
        {
            var ids = new HashSet<long>();
            WithPanelHidden(() =>
            {
                foreach (Element el in Pick("Pick elements - their circuits get selected - then click Finish"))
                {
                    if (el is ElectricalSystem es) { ids.Add(Rvt.Val(es.Id)); continue; }
                    foreach (ElectricalSystem s in Rvt.ElementSystems(el)) ids.Add(Rvt.Val(s.Id));
                }
            });
            SelectTag(cmbFilterPanel, "__all__");
            txtSearch.Text = "";
            FillCircuits(ids);
            tabs.SelectedIndex = 1;
            Log($"Select: {ids.Count} circuit(s) found on the picked elements.");
            RefreshLog();
        }

        private void OnRefresh()
        {
            FillPanelCombos();
            RefreshAll();
        }

        private void OnMove()
        {
            var circuits = SelectedCircuits();
            var panel = Rvt.Get(_doc, Tag(cmbMovePanel)) as FamilyInstance;
            if (circuits.Count == 0 || panel == null)
            {
                Log("Move: select circuits and a target panel first.");
                RefreshLog();
                return;
            }
            int ok = 0, bad = 0;
            Rvt.InTx(_doc, "Move circuits", () =>
            {
                foreach (ElectricalSystem c in circuits)
                {
                    string label = Rvt.CircuitLabel(c);
                    try { c.SelectPanel(panel); ok++; }
                    catch (Exception ex) { bad++; Log($"  x {label}: {ex.Message}"); }
                }
            });
            Log($"Move: {ok} circuit(s) moved to {Rvt.PanelName(panel)}" + (bad > 0 ? $", {bad} failed" : "") + ".");
            RefreshAll();
        }

        private void OnDisconnect()
        {
            var circuits = SelectedCircuits();
            if (circuits.Count == 0)
            {
                Log("Disconnect: select circuits first.");
                RefreshLog();
                return;
            }
            int ok = 0, bad = 0;
            Rvt.InTx(_doc, "Disconnect circuits", () =>
            {
                foreach (ElectricalSystem c in circuits)
                {
                    if (c.BaseEquipment == null) continue;
                    try { c.DisconnectPanel(); ok++; }
                    catch (Exception ex) { bad++; Log($"  x {Rvt.CircuitLabel(c)}: {ex.Message}"); }
                }
            });
            Log($"Disconnect: {ok} circuit(s) disconnected from their panel" + (bad > 0 ? $", {bad} failed" : "") + ".");
            RefreshAll();
        }

        private void OnEditSelected()
        {
            var tags = SelectedTags(lstCircuits);
            if (tags.Count == 0) return;
            SelectTag(cmbEditCircuit, tags[0]);
            FillEditElems();
            tabs.SelectedIndex = 2;
        }

        private void OnDelete()
        {
            var circuits = SelectedCircuits();
            if (circuits.Count == 0)
            {
                Log("Delete: select circuits first.");
                RefreshLog();
                return;
            }
            if (Ask($"Delete {circuits.Count} circuit(s)?\n\nThe fixtures stay in the model, only the circuits are removed. "
                    + "You can Undo this in Revit.") != MessageBoxResult.Yes) return;
            int ok = 0, bad = 0;
            Rvt.InTx(_doc, "Delete circuits", () =>
            {
                foreach (ElectricalSystem c in circuits)
                {
                    string label = Rvt.CircuitLabel(c);
                    try { _doc.Delete(c.Id); ok++; }
                    catch (Exception ex) { bad++; Log($"  x {label}: {ex.Message}"); }
                }
            });
            Log($"Delete: {ok} circuit(s) deleted" + (bad > 0 ? $", {bad} failed" : "") + ".");
            RefreshAll();
        }

        // ================================================================= TAB 3
        private void OnAddElems()
        {
            if (!(Rvt.Get(_doc, Tag(cmbEditCircuit)) is ElectricalSystem c))
            {
                Log("Add: choose a circuit first.");
                RefreshLog();
                return;
            }
            List<Element> picked = null;
            WithPanelHidden(() => picked = Pick("Pick elements to ADD to the circuit, then click Finish")
                .Where(Rvt.IsCircuitable).ToList());
            if (picked.Count == 0)
            {
                Log("Add: nothing picked.");
            }
            else
            {
                try
                {
                    Rvt.InTx(_doc, "Add elements to circuit", () => c.AddToCircuit(Rvt.MakeSet(picked)));
                    Log($"Add: {picked.Count} element(s) added to {Rvt.CircuitLabel(c)}.");
                }
                catch (Exception ex)
                {
                    Log("Add failed: " + ex.Message);
                }
            }
            tabs.SelectedIndex = 2;
            RefreshAll();
        }

        private void OnRemoveElems()
        {
            var c = Rvt.Get(_doc, Tag(cmbEditCircuit)) as ElectricalSystem;
            var elems = SelectedTags(lstEditElems).Select(t => Rvt.Get(_doc, t)).Where(e => e != null).ToList();
            if (c == null || elems.Count == 0)
            {
                Log("Remove: choose a circuit and select elements in the list.");
                RefreshLog();
                return;
            }
            try
            {
                Rvt.InTx(_doc, "Remove elements from circuit", () => c.RemoveFromCircuit(Rvt.MakeSet(elems)));
                Log($"Remove: {elems.Count} element(s) removed from {Rvt.CircuitLabel(c)}.");
            }
            catch (Exception ex)
            {
                Log("Remove failed: " + ex.Message);
            }
            RefreshAll();
        }

        // ============================================ TAB 4 - load classification check
        private void LcRun(bool writeLog)
        {
            if (!(Rvt.Get(_doc, Tag(cmbLcPanel)) is FamilyInstance panel))
            {
                txtLcVerdict.Text = "Choose a panel first.";
                return;
            }
            LcResult res = LoadClass.Run(_doc, panel, chkLcDown.IsChecked == true, chkLcDeep.IsChecked == true, _famCache);
            ShowLc(res);
            if (writeLog)
            {
                Log($"Load classification check of '{res.Panel}':");
                foreach (string line in res.Verdict) Log("   " + line);
            }
            RefreshLog();
        }

        private void ShowLc(LcResult res)
        {
            _lc = res;
            _lcGroupIds = new Dictionary<string, List<long>>();
            lstLcRows.Items.Clear();
            for (int gi = 0; gi < res.Order.Count; gi++)
            {
                string k = res.Order[gi];
                var rows = res.Groups[k];
                string gtag = "g:" + gi;
                _lcGroupIds[gtag] = rows.Where(r => r.Kind != "feeder").Select(r => r.Id).ToList();
                bool red = k != LoadClass.KeyInfo
                           && (!LoadClass.IsIdKey(k) || res.Dups.ContainsKey(LoadClass.IdOf(k)));
                var head = ListItem($"■  {res.Labels[k]}    [{rows.Count}]", gtag);
                head.FontWeight = FontWeights.Bold;
                head.Background = Color(red ? "#FDE7E5" : "#E6EEF9");
                lstLcRows.Items.Add(head);
                foreach (LcRow r in rows)
                {
                    string txt = $"      {r.Ckt}    {r.Label}\n          via {(r.How.Count > 0 ? string.Join(";  ", r.How) : "-")}";
                    foreach (string prob in r.Problems) txt += "\n          ⚠ " + prob;
                    var it = ListItem(txt, r.Id);
                    if (r.Problems.Count > 0) it.Foreground = Brushes.Firebrick;
                    lstLcRows.Items.Add(it);
                }
            }
            txtLcVerdict.Text = string.Join("\n", res.Verdict);
            bdLcVerdict.Background = Color(res.Ok == true ? "#E3F6EA" : res.Ok == false ? "#FDE7E5" : "#EEF2F8");
            FillLcProject(res.Lcs, res.Usage);
            object prefer = res.Usage.Count > 0 ? (object)res.Usage.OrderByDescending(kv => kv.Value).First().Key : null;
            FillLcTarget(res.Lcs, prefer);
        }

        private List<long> LcSelectedIds()
        {
            var ids = new List<long>();
            foreach (object t in SelectedTags(lstLcRows))
            {
                IEnumerable<long> more = t is string g ? (_lcGroupIds.TryGetValue(g, out var l) ? l : new List<long>())
                                       : t is long v ? new[] { v } : new long[0];
                foreach (long v2 in more)
                    if (!ids.Contains(v2)) ids.Add(v2);
            }
            return ids;
        }

        private void LcApply(List<long> ids)
        {
            if (_lc == null)
            {
                Log("Fix: run the check first.");
                RefreshLog();
                return;
            }
            object tgt = Tag(cmbLcTarget);
            var rows = _lc.Rows.Where(r => r.Kind != "feeder").GroupBy(r => r.Id).ToDictionary(g => g.Key, g => g.First());
            ids = ids.Where(rows.ContainsKey).ToList();
            if (!(tgt is long target) || ids.Count == 0)
            {
                Log("Fix: select rows and a load classification first.");
                RefreshLog();
                return;
            }
            var lcs = LoadClass.All(_doc);
            ElementId tgtId = Rvt.Id(target);
            var doneTypes = new HashSet<long>();
            int changed = 0, inFamily = 0, failed = 0, types = 0;
            Rvt.InTx(_doc, "Unify load classification", () =>
            {
                foreach (long v in ids)
                {
                    Element el = _doc.GetElement(Rvt.Id(v));
                    if (el == null) continue;
                    var prms = LoadClass.LcParams(el, lcs);
                    List<string> drive = rows[v].Drive;
                    if (drive != null) prms = prms.Where(wp => drive.Contains(wp.Value.Definition.Name)).ToList();
                    if (prms.Count == 0)
                    {
                        inFamily++;
                        Log($"  ! {rows[v].Label}: the load classification is fixed inside the family - edit the family's electrical connector.");
                        continue;
                    }
                    foreach (var wp in prms)
                    {
                        Parameter p = wp.Value;
                        if (wp.Key == "type" && !doneTypes.Add(Rvt.Val(((FamilyInstance)el).Symbol.Id))) continue;
                        if (p.IsReadOnly)
                        {
                            failed++;
                            Log($"  x {rows[v].Label}: '{p.Definition.Name}' is read-only");
                            continue;
                        }
                        if (Rvt.Val(p.AsElementId()) == target) continue;
                        try
                        {
                            p.Set(tgtId);
                            changed++;
                            if (wp.Key == "type") types++;
                        }
                        catch (Exception ex)
                        {
                            failed++;
                            Log($"  x {rows[v].Label}: {ex.Message}");
                        }
                    }
                }
            });
            string msg = $"Fix: {changed} parameter(s) set to {(lcs.ContainsKey(target) ? LoadClass.Visible(lcs[target].Name) : target.ToString())}";
            if (types > 0) msg += $" ({types} on types - every instance of those types changes, also on other panels)";
            if (inFamily > 0) msg += $", {inFamily} fixed inside the family";
            if (failed > 0) msg += $", {failed} failed";
            Log(msg + ".");
            LcRun(false);
            RefreshAll();
        }

        private void OnLcSelect()
        {
            var ids = LcSelectedIds().Select(Rvt.Id).ToList();
            _uidoc.Selection.SetElementIds(ids);
            Log($"Select: {ids.Count} element(s) selected in Revit.");
            RefreshLog();
        }

        // ------------------------------------------- the project's load classifications
        private void FillLcProject(Dictionary<long, ElectricalLoadClassification> lcs, Dictionary<long, int> usage)
        {
            var keep = new HashSet<long>(SelectedTags(lstLcProject).OfType<long>());
            lstLcProject.Items.Clear();
            var dups = LoadClass.Duplicates(lcs);
            var refs = LoadClass.ProjectRefs(_doc, lcs);
            _lcUse = lcs.Keys.ToDictionary(v => v, v => refs.TryGetValue(v, out var l) ? l.Count : 0);
            foreach (var kv in LoadClass.Sorted(lcs))
            {
                long v = kv.Key;
                string txt = $"{LoadClass.Visible(kv.Value.Name)}   id {v}   - used {_lcUse[v]}x";
                if (usage != null) txt += $", {(usage.TryGetValue(v, out int u) ? u : 0)} on panel";
                if (dups.ContainsKey(v)) txt += "\n      same name as id " + string.Join(", ", dups[v]);
                var it = ListItem(txt, v);
                if (dups.ContainsKey(v))
                {
                    it.Foreground = Brushes.Firebrick;
                    it.FontWeight = FontWeights.Bold;
                }
                lstLcProject.Items.Add(it);
                if (keep.Contains(v)) it.IsSelected = true;
            }
        }

        private void FillLcTarget(Dictionary<long, ElectricalLoadClassification> lcs, object prefer)
        {
            object keepFix = prefer ?? Tag(cmbLcTarget), keepMerge = Tag(cmbLcMerge);
            cmbLcTarget.Items.Clear();
            cmbLcMerge.Items.Clear();
            foreach (var kv in LoadClass.Sorted(lcs))
            {
                string label = $"{LoadClass.Visible(kv.Value.Name)}   (id {kv.Key})";
                cmbLcTarget.Items.Add(ComboItem(label, kv.Key));
                cmbLcMerge.Items.Add(ComboItem(label, kv.Key));
            }
            SelectTag(cmbLcTarget, keepFix);
            SelectTag(cmbLcMerge, keepMerge);
        }

        private void FillLcDemand()
        {
            cmbLcDemand.Items.Clear();
            cmbLcDemand.Items.Add(ComboItem("(none)", -1L));
            foreach (var kv in LoadClass.DemandFactors(_doc)) cmbLcDemand.Items.Add(ComboItem(kv.Value, kv.Key));
        }

        private List<long> LcProjectSelection()
            => SelectedTags(lstLcProject).OfType<long>().Where(v => _doc.GetElement(Rvt.Id(v)) != null).ToList();

        private void OnLcProjectSelection()
        {
            var sel = LcProjectSelection();
            if (sel.Count == 0) return;
            var lc = (ElectricalLoadClassification)_doc.GetElement(Rvt.Id(sel[0]));
            txtLcName.Text = lc.Name;
            txtLcAbbr.Text = lc.Abbreviation ?? "";
            SelectTag(cmbLcDemand, Rvt.Val(lc.DemandFactorId));
        }

        private void AfterLcEdit(long? select)
        {
            if (select != null) lstLcProject.UnselectAll();
            if (_lc != null) LcRun(false);           // refresh the check with the new names
            else
            {
                var lcs = LoadClass.All(_doc);
                FillLcProject(lcs, null);
                FillLcTarget(lcs, null);
            }
            if (select != null)
            {
                foreach (ListBoxItem it in lstLcProject.Items)
                {
                    if (!Equals(it.Tag, select.Value)) continue;
                    it.IsSelected = true;
                    lstLcProject.ScrollIntoView(it);
                }
                OnLcProjectSelection();
            }
            RefreshLog();
        }

        private void OnLcSave()
        {
            var sel = LcProjectSelection();
            if (sel.Count == 0)
            {
                Log("Edit: click a load classification in the list first.");
                RefreshLog();
                return;
            }
            long v = sel[0];
            var lc = (ElectricalLoadClassification)_doc.GetElement(Rvt.Id(v));
            string name = txtLcName.Text ?? "";
            if (name.Trim().Length == 0)
            {
                Log("Edit: the name cannot be empty.");
                RefreshLog();
                return;
            }
            var lcs = LoadClass.All(_doc);
            long? clash = LoadClass.NameConflict(name, lcs, v);
            if (clash != null)
            {
                Log($"Edit: {LoadClass.Visible(name)} would look the same as id {clash} {LoadClass.Visible(lcs[clash.Value].Name)} - "
                    + "use New / merge > Merge instead.");
                RefreshLog();
                return;
            }
            string old = lc.Name;
            Rvt.InTx(_doc, "Edit load classification", () =>
            {
                if (lc.Name != name) lc.Name = name;
                string abbr = txtLcAbbr.Text ?? "";
                try { if ((lc.Abbreviation ?? "") != abbr) lc.Abbreviation = abbr; }
                catch (Exception ex) { Log("  ! abbreviation not changed: " + ex.Message); }
                try
                {
                    if (Tag(cmbLcDemand) is long dv && Rvt.Val(lc.DemandFactorId) != dv)
                        lc.DemandFactorId = dv < 0 ? ElementId.InvalidElementId : Rvt.Id(dv);
                }
                catch (Exception ex) { Log("  ! demand factor not changed: " + ex.Message); }
            });
            Log($"Edit: load classification {LoadClass.Visible(old)} (id {v}) saved as {LoadClass.Visible(name)}.");
            AfterLcEdit(v);
        }

        private void OnLcClean()
        {
            var lcs = LoadClass.All(_doc);
            var sel = LcProjectSelection();
            if (sel.Count == 0) sel = lcs.Keys.ToList();      // nothing selected = all
            int done = 0;
            var clashes = new List<string>();
            Rvt.InTx(_doc, "Clean load classification names", () =>
            {
                foreach (long v in sel)
                {
                    ElectricalLoadClassification lc = lcs[v];
                    string clean = LoadClass.Clean(lc.Name);
                    if (clean.Length == 0 || clean == lc.Name) continue;
                    var other = lcs.Where(kv => kv.Key != v && kv.Value.Name == clean).Select(kv => kv.Key).ToList();
                    if (other.Count > 0)
                    {
                        clashes.Add($"{LoadClass.Visible(lc.Name)} (id {v}) -> {LoadClass.Visible(clean)} already exists as id {other[0]}");
                        continue;
                    }
                    string old = lc.Name;
                    lc.Name = clean;
                    done++;
                    Log($"  renamed {LoadClass.Visible(old)} -> {LoadClass.Visible(clean)} (id {v})");
                }
            });
            Log($"Clean names: {done} renamed"
                + (clashes.Count > 0 ? $", {clashes.Count} could not be renamed because the clean name already exists - merge them instead" : "")
                + ".");
            foreach (string c in clashes) Log("  ! " + c);
            AfterLcEdit(null);
        }

        private void OnLcDelete()
        {
            var sel = LcProjectSelection();
            if (sel.Count == 0)
            {
                Log("Delete: select load classification(s) in the list first.");
                RefreshLog();
                return;
            }
            var lcs = LoadClass.All(_doc);
            var used = sel.Where(v => _lcUse.TryGetValue(v, out int n) && n > 0).ToList();
            var free = sel.Except(used).ToList();
            foreach (long v in used)
                Log($"  ! {LoadClass.Visible(lcs[v].Name)} (id {v}) is used by {_lcUse[v]} parameter(s) - use New / merge > Merge to move them first.");
            if (free.Count == 0)
            {
                RefreshLog();
                return;
            }
            if (Ask($"Delete {free.Count} load classification(s)?\n\n{string.Join("\n", free.Select(v => lcs[v].Name))}\n\n"
                    + "No element parameter uses them. A family that has one fixed inside the family is not counted - run the "
                    + "check afterwards. You can Undo this in Revit.") != MessageBoxResult.Yes)
            {
                RefreshLog();
                return;
            }
            int ok = 0;
            Rvt.InTx(_doc, "Delete load classifications", () =>
            {
                foreach (long v in free)
                {
                    try { _doc.Delete(Rvt.Id(v)); ok++; }
                    catch (Exception ex) { Log($"  x {LoadClass.Visible(lcs[v].Name)} (id {v}): {ex.Message}"); }
                }
            });
            Log($"Delete: {ok} load classification(s) deleted.");
            AfterLcEdit(null);
        }

        private void OnLcNew()
        {
            string name = txtLcNewName.Text ?? "";
            if (name.Trim().Length == 0)
            {
                Log("New: type a name first.");
                RefreshLog();
                return;
            }
            var lcs = LoadClass.All(_doc);
            long? clash = LoadClass.NameConflict(name, lcs);
            if (clash != null)
            {
                Log($"New: {LoadClass.Visible(lcs[clash.Value].Name)} already exists (id {clash}).");
                RefreshLog();
                return;
            }
            ElectricalLoadClassification created = null;
            Rvt.InTx(_doc, "New load classification", () =>
            {
                created = ElectricalLoadClassification.Create(_doc, name);
                string abbr = txtLcNewAbbr.Text ?? "";
                if (abbr.Length > 0)
                {
                    try { created.Abbreviation = abbr; }
                    catch (Exception ex) { Log("  ! abbreviation not set: " + ex.Message); }
                }
            });
            long id = Rvt.Val(created.Id);
            Log($"New: load classification {LoadClass.Visible(name)} created (id {id}).");
            txtLcNewName.Text = "";
            txtLcNewAbbr.Text = "";
            AfterLcEdit(id);
        }

        private void OnLcMerge()
        {
            var lcs = LoadClass.All(_doc);
            if (!(Tag(cmbLcMerge) is long tgt) || !lcs.ContainsKey(tgt))
            {
                Log("Merge: choose the classification to keep in the Merge combo.");
                RefreshLog();
                return;
            }
            var srcs = LcProjectSelection().Where(v => v != tgt).ToList();
            if (srcs.Count == 0)
            {
                Log("Merge: select the classification(s) to remove in the list first.");
                RefreshLog();
                return;
            }
            string list = string.Join("\n", srcs.Select(v => $"{LoadClass.Visible(lcs[v].Name)}  (id {v})"));
            if (Ask($"Move every load, type and circuit that uses\n\n{list}\n\nto  {LoadClass.Visible(lcs[tgt].Name)}  and then delete "
                    + "the classification(s) above?\n\nA family that has one fixed inside the family cannot be changed from here - "
                    + "run the check afterwards. You can Undo this in Revit.") != MessageBoxResult.Yes) return;

            var refs = LoadClass.ProjectRefs(_doc, lcs);
            ElementId tgtId = Rvt.Id(tgt);
            int moved = 0, readOnly = 0, deleted = 0;
            Rvt.InTx(_doc, "Merge load classifications", () =>
            {
                foreach (long v in srcs)
                {
                    if (!refs.TryGetValue(v, out var uses)) continue;
                    foreach (var op in uses)
                    {
                        if (op.Value.IsReadOnly) { readOnly++; continue; }
                        try { op.Value.Set(tgtId); moved++; }
                        catch (Exception ex)
                        {
                            readOnly++;
                            Log($"  x id {Rvt.Val(op.Key.Id)} '{op.Value.Definition.Name}': {ex.Message}");
                        }
                    }
                }
                foreach (long v in srcs)
                {
                    try { _doc.Delete(Rvt.Id(v)); deleted++; }
                    catch (Exception ex) { Log($"  ! could not delete {LoadClass.Visible(lcs[v].Name)} (id {v}): {ex.Message}"); }
                }
            });
            string msg = $"Merge: {moved} parameter(s) moved to {LoadClass.Visible(lcs[tgt].Name)}, {deleted} classification(s) deleted";
            if (readOnly > 0) msg += $", {readOnly} read-only parameter(s) not changed";
            Log(msg + ".");
            AfterLcEdit(tgt);
        }
    }
}
