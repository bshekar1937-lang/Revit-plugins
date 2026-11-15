// -----------------------------------------------------------------------------
// Drawing the routes as red detail lines. This is the only part of the add-in
// that changes the model, and it does it all in one transaction, so a single
// undo removes everything a run created.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;
using PanelCableRoute.Core;

namespace PanelCableRoute.Revit
{
    /// <summary>What a drawing run did.</summary>
    public class DrawingSummary
    {
        public bool Committed;
        public int Created;
        public int Deleted;
        public int Views;
        public List<string> Errors = new List<string>();
        public List<long> MissingLevels = new List<long>();
        public string Failure = "";
    }

    public static class RouteDrawing
    {
        public const string StyleName = "Auto Cable Route - Red";

        /// <summary>The plan view types a detail line can be drawn in.</summary>
        public static bool IsPlanView(View view)
        {
            if (view == null)
            {
                return false;
            }
            return view.ViewType == ViewType.FloorPlan
                || view.ViewType == ViewType.CeilingPlan
                || view.ViewType == ViewType.EngineeringPlan;
        }

        /// <summary>
        /// The red line subcategory the routes are drawn with, created on the
        /// first run. Must be called inside a transaction.
        /// </summary>
        public static GraphicsStyle RedStyle(Document doc)
        {
            Category lines = Category.GetCategory(doc, BuiltInCategory.OST_Lines);
            if (lines == null)
            {
                return null;
            }
            foreach (Category subcategory in lines.SubCategories)
            {
                if (string.Equals(subcategory.Name, StyleName,
                        StringComparison.Ordinal))
                {
                    return subcategory.GetGraphicsStyle(
                        GraphicsStyleType.Projection);
                }
            }
            Category created = doc.Settings.Categories.NewSubcategory(
                lines, StyleName);
            created.LineColor = new Color(255, 0, 0);
            created.SetLineWeight(6, GraphicsStyleType.Projection);
            return created.GetGraphicsStyle(GraphicsStyleType.Projection);
        }

        /// <summary>Flatten a model point onto the plane of a plan view.</summary>
        public static XYZ ProjectToView(XYZ point, View view)
        {
            XYZ origin = view.Origin;
            XYZ normal = view.ViewDirection.Normalize();
            return point - normal.Multiply(point.Subtract(origin).DotProduct(normal));
        }

        /// <summary>
        /// Drop duplicate points, straighten collinear runs and remove stubs
        /// shorter than Revit will accept as a curve.
        /// </summary>
        public static List<XYZ> Simplify(
            List<XYZ> points, double minimumLength, double tolerance)
        {
            List<XYZ> cleaned = new List<XYZ>();
            for (int index = 0; index < points.Count; index++)
            {
                if (cleaned.Count == 0
                        || cleaned[cleaned.Count - 1].DistanceTo(points[index])
                            > tolerance)
                {
                    cleaned.Add(points[index]);
                }
            }

            int cursor = 1;
            while (cursor < cleaned.Count - 1)
            {
                XYZ first = cleaned[cursor] - cleaned[cursor - 1];
                XYZ second = cleaned[cursor + 1] - cleaned[cursor];
                bool collinear = first.DotProduct(second) >= 0.0
                    && first.CrossProduct(second).GetLength()
                        <= tolerance * (first.GetLength() + second.GetLength());
                if (collinear)
                {
                    cleaned.RemoveAt(cursor);
                }
                else
                {
                    cursor++;
                }
            }

            cursor = 0;
            while (cursor < cleaned.Count - 1)
            {
                if (cleaned[cursor].DistanceTo(cleaned[cursor + 1])
                        >= minimumLength)
                {
                    cursor++;
                    continue;
                }
                if (cursor == 0)
                {
                    cleaned.RemoveAt(1);
                }
                else if (cursor + 1 == cleaned.Count - 1)
                {
                    cleaned.RemoveAt(cursor);
                    cursor = cursor > 0 ? cursor - 1 : 0;
                }
                else
                {
                    cleaned.RemoveAt(cursor + 1);
                }
            }
            return cleaned;
        }

        /// <summary>Detail lines a previous run drew in this view.</summary>
        private static List<ElementId> PreviousLines(
            Document doc, View view, GraphicsStyle style)
        {
            List<ElementId> found = new List<ElementId>();
            FilteredElementCollector collector =
                new FilteredElementCollector(doc, view.Id)
                    .OfClass(typeof(CurveElement));
            foreach (Element element in collector)
            {
                CurveElement curve = element as CurveElement;
                if (curve == null)
                {
                    continue;
                }
                try
                {
                    if (curve.ViewSpecific
                            && curve.OwnerViewId == view.Id
                            && curve.LineStyle != null
                            && curve.LineStyle.Id == style.Id)
                    {
                        found.Add(curve.Id);
                    }
                }
                catch (Exception)
                {
                }
            }
            return found;
        }

        /// <summary>
        /// The active plan, plus one plan view for each end's level, so a
        /// route between levels appears on both.
        /// </summary>
        public static List<View> RouteViews(
            Document doc, View activeView, Element source, Element destination,
            out List<long> missingLevels)
        {
            Dictionary<long, View> views = new Dictionary<long, View>();
            views[ModelReader.IdOf(activeView)] = activeView;
            HashSet<long> wanted = new HashSet<long>();
            Element[] ends = new Element[] { source, destination };
            for (int index = 0; index < ends.Length; index++)
            {
                ElementId levelId = ModelReader.LevelIdOf(ends[index]);
                if (levelId != null)
                {
                    wanted.Add(ModelReader.IdOf(levelId));
                }
            }
            missingLevels = new List<long>();
            if (wanted.Count == 0)
            {
                return new List<View>(views.Values);
            }

            List<ViewPlan> matching = new List<ViewPlan>();
            FilteredElementCollector collector =
                new FilteredElementCollector(doc).OfClass(typeof(ViewPlan));
            foreach (Element element in collector)
            {
                ViewPlan plan = element as ViewPlan;
                if (plan == null)
                {
                    continue;
                }
                try
                {
                    if (plan.IsTemplate
                            || plan.ViewType != activeView.ViewType
                            || plan.GenLevel == null
                            || !wanted.Contains(
                                ModelReader.IdOf(plan.GenLevel.Id)))
                    {
                        continue;
                    }
                    matching.Add(plan);
                }
                catch (Exception)
                {
                }
            }

            HashSet<long> covered = new HashSet<long>();
            try
            {
                ViewPlan activePlan = activeView as ViewPlan;
                if (activePlan != null && activePlan.GenLevel != null)
                {
                    long activeLevel = ModelReader.IdOf(activePlan.GenLevel.Id);
                    if (wanted.Contains(activeLevel))
                    {
                        covered.Add(activeLevel);
                    }
                }
            }
            catch (Exception)
            {
            }
            matching.Sort(delegate (ViewPlan a, ViewPlan b)
            {
                return string.Compare(a.Name, b.Name,
                    StringComparison.OrdinalIgnoreCase);
            });
            for (int index = 0; index < matching.Count; index++)
            {
                long levelId = ModelReader.IdOf(matching[index].GenLevel.Id);
                if (covered.Contains(levelId))
                {
                    continue;
                }
                views[ModelReader.IdOf(matching[index])] = matching[index];
                covered.Add(levelId);
            }
            foreach (long levelId in wanted)
            {
                if (!covered.Contains(levelId))
                {
                    missingLevels.Add(levelId);
                }
            }
            missingLevels.Sort();
            return new List<View>(views.Values);
        }

        /// <summary>
        /// Draw these routes, replacing the lines a previous run left in the
        /// same views. Everything is one transaction: on any failure nothing
        /// is left behind.
        /// </summary>
        public static DrawingSummary Draw(
            Document doc, View activeView, IList<RouteRecord> routes)
        {
            DrawingSummary summary = new DrawingSummary();
            using (Transaction transaction =
                new Transaction(doc, "Draw panel cable routes"))
            {
                transaction.Start();
                try
                {
                    GraphicsStyle style = RedStyle(doc);
                    if (style == null)
                    {
                        throw new Exception(
                            "The Lines category could not be read, so the red "
                            + "line style could not be created.");
                    }

                    Dictionary<RouteRecord, List<View>> viewsByRoute =
                        new Dictionary<RouteRecord, List<View>>();
                    Dictionary<long, View> allViews = new Dictionary<long, View>();
                    allViews[ModelReader.IdOf(activeView)] = activeView;
                    HashSet<long> missing = new HashSet<long>();
                    for (int index = 0; index < routes.Count; index++)
                    {
                        RouteRecord route = routes[index];
                        Element source = doc.GetElement(
                            ModelReader.ToElementId(route.Source.Id));
                        Element destination = doc.GetElement(
                            ModelReader.ToElementId(route.Destination.Id));
                        List<long> routeMissing;
                        List<View> views = RouteViews(
                            doc, activeView, source, destination,
                            out routeMissing);
                        viewsByRoute[route] = views;
                        for (int item = 0; item < views.Count; item++)
                        {
                            allViews[ModelReader.IdOf(views[item])] = views[item];
                        }
                        for (int item = 0; item < routeMissing.Count; item++)
                        {
                            missing.Add(routeMissing[item]);
                        }
                    }
                    summary.Views = allViews.Count;
                    summary.MissingLevels = new List<long>(missing);
                    summary.MissingLevels.Sort();

                    List<ElementId> stale = new List<ElementId>();
                    foreach (View view in allViews.Values)
                    {
                        stale.AddRange(PreviousLines(doc, view, style));
                    }

                    double shortCurve = doc.Application.ShortCurveTolerance;
                    for (int index = 0; index < routes.Count; index++)
                    {
                        RouteRecord route = routes[index];
                        List<View> views = viewsByRoute[route];
                        for (int item = 0; item < views.Count; item++)
                        {
                            View view = views[item];
                            List<XYZ> flattened = new List<XYZ>();
                            for (int step = 0; step < route.Points.Count; step++)
                            {
                                flattened.Add(ProjectToView(
                                    ModelReader.ToXyz(route.Points[step]),
                                    view));
                            }
                            List<XYZ> cleaned = Simplify(
                                flattened, shortCurve, 0.001);
                            int drawn = 0;
                            for (int step = 0; step < cleaned.Count - 1; step++)
                            {
                                if (cleaned[step].DistanceTo(cleaned[step + 1])
                                        < shortCurve)
                                {
                                    continue;
                                }
                                try
                                {
                                    DetailCurve detail = doc.Create.NewDetailCurve(
                                        view,
                                        Line.CreateBound(cleaned[step],
                                            cleaned[step + 1]));
                                    detail.LineStyle = style;
                                    summary.Created++;
                                    drawn++;
                                }
                                catch (Exception error)
                                {
                                    if (summary.Errors.Count < 10)
                                    {
                                        summary.Errors.Add(route.Title
                                            + " in " + view.Name + ": "
                                            + error.Message);
                                    }
                                }
                            }
                            if (drawn == 0 && summary.Errors.Count < 10)
                            {
                                summary.Errors.Add(route.Title + " in "
                                    + view.Name
                                    + ": nothing was left to draw after "
                                    + "flattening onto the view.");
                            }
                        }
                    }

                    if (summary.Created == 0)
                    {
                        throw new Exception(summary.Errors.Count > 0
                            ? summary.Errors[0]
                            : "Revit did not accept any of the route segments.");
                    }
                    for (int index = 0; index < stale.Count; index++)
                    {
                        try
                        {
                            doc.Delete(stale[index]);
                            summary.Deleted++;
                        }
                        catch (Exception)
                        {
                        }
                    }
                    transaction.Commit();
                    summary.Committed = true;
                }
                catch (Exception error)
                {
                    if (transaction.GetStatus() == TransactionStatus.Started)
                    {
                        transaction.RollBack();
                    }
                    summary.Failure = error.Message;
                }
            }
            return summary;
        }
    }
}
