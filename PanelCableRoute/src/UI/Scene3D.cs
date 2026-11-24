// -----------------------------------------------------------------------------
// The 3D preview, built with WPF 3D inside the dialog. Nothing is drawn in the
// Revit model: this is a picture of what was measured.
//
// Boxes of one colour share a single mesh, so a few thousand route segments
// stay a handful of objects to draw.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;
using System.Windows.Media;
using System.Windows.Media.Media3D;
using PanelCableRoute.Core;

namespace PanelCableRoute.UI
{
    /// <summary>What to draw in the preview.</summary>
    public class SceneOptions
    {
        public bool ShowTrays = true;
        public bool ShowPanels = true;
        public bool ShowAllRoutes = true;
        public RouteRecord Picked;
    }

    /// <summary>The scene, and the bounds the camera should frame.</summary>
    public class Scene
    {
        public Model3DGroup Content;
        public bool HasBounds;
        public Pt Low;
        public Pt High;
        public int RouteCount;
        public int PanelCount;
        public int TrayCount;
        public bool Capped;
    }

    public static class Scene3D
    {
        // Colours. Keep in step with the legend swatches in ResultWindow.xaml.
        private static readonly Color TrayColour =
            Color.FromArgb(80, 0x94, 0xA3, 0xB8);
        private static readonly Color PanelColour =
            Color.FromArgb(235, 0x25, 0x63, 0xEB);
        private static readonly Color RouteColour =
            Color.FromArgb(210, 0x22, 0xD3, 0xEE);
        private static readonly Color OverColour =
            Color.FromArgb(235, 0xEF, 0x44, 0x44);
        private static readonly Color PickedColour =
            Color.FromArgb(255, 0xFA, 0xCC, 0x15);

        public const double RouteTubeMm = 45.0;
        public const double PickedTubeMm = 110.0;
        public const int MaxBoxes = 24000;

        /// <summary>The two triangles of each face of a box.</summary>
        private static readonly int[] BoxTriangles = new int[]
        {
            0, 1, 5, 0, 5, 4,
            3, 7, 6, 3, 6, 2,
            0, 4, 7, 0, 7, 3,
            1, 2, 6, 1, 6, 5,
            0, 3, 2, 0, 2, 1,
            4, 5, 6, 4, 6, 7
        };

        public static Scene Build(
            IList<RouteRecord> routes, IEnumerable<TraySegment> trays,
            SceneOptions options)
        {
            Scene scene = new Scene();
            MeshGeometry3D trayMesh = new MeshGeometry3D();
            MeshGeometry3D panelMesh = new MeshGeometry3D();
            MeshGeometry3D routeMesh = new MeshGeometry3D();
            MeshGeometry3D overMesh = new MeshGeometry3D();
            MeshGeometry3D pickedMesh = new MeshGeometry3D();

            Bounds frame = new Bounds();        // routes and panels only
            Bounds trayBounds = new Bounds();   // the fallback, if nothing else
            int boxes = 0;

            List<RouteRecord> drawn = new List<RouteRecord>();
            if (options.ShowAllRoutes)
            {
                for (int index = 0; index < routes.Count; index++)
                {
                    drawn.Add(routes[index]);
                }
            }
            else if (options.Picked != null)
            {
                drawn.Add(options.Picked);
            }

            for (int index = 0; index < drawn.Count; index++)
            {
                RouteRecord route = drawn[index];
                bool isPicked = ReferenceEquals(route, options.Picked);
                MeshGeometry3D target = isPicked
                    ? pickedMesh
                    : (route.WithinLimit ? routeMesh : overMesh);
                double half = Units.MmToFeet(
                    isPicked ? PickedTubeMm : RouteTubeMm) / 2.0;
                boxes += AddPolyline(target, route.Points, half, frame,
                    MaxBoxes - boxes);
                if (boxes >= MaxBoxes)
                {
                    scene.Capped = true;
                    break;
                }
            }
            scene.RouteCount = drawn.Count;

            if (options.ShowPanels)
            {
                HashSet<long> seen = new HashSet<long>();
                IList<RouteRecord> forPanels = drawn.Count > 0 ? drawn : routes;
                for (int index = 0; index < forPanels.Count; index++)
                {
                    RouteRecord route = forPanels[index];
                    EquipmentItem[] ends = new EquipmentItem[]
                    {
                        route.Source, route.Destination
                    };
                    for (int end = 0; end < ends.Length; end++)
                    {
                        EquipmentItem item = ends[end];
                        if (!seen.Add(item.Id))
                        {
                            continue;
                        }
                        Pt[] corners = ItemCorners(item);
                        if (corners == null)
                        {
                            continue;
                        }
                        AddBox(panelMesh, corners);
                        frame.Include(corners);
                        scene.PanelCount++;
                    }
                }
            }

            if (options.ShowTrays && trays != null)
            {
                foreach (TraySegment tray in trays)
                {
                    if (boxes >= MaxBoxes)
                    {
                        scene.Capped = true;
                        break;
                    }
                    Pt normal;
                    Pt up;
                    if (!Geometry.TraySection(
                            tray.Start, tray.End,
                            Units.MmToFeet(tray.WidthMm) / 2.0,
                            Units.MmToFeet(tray.HeightMm) / 2.0,
                            out normal, out up))
                    {
                        continue;
                    }
                    Pt[] corners = Geometry.PrismCorners(
                        tray.Start, tray.End, normal, up);
                    AddBox(trayMesh, corners);
                    trayBounds.Include(corners);
                    scene.TrayCount++;
                    boxes++;
                }
            }

            Model3DGroup group = new Model3DGroup();
            group.Children.Add(new AmbientLight(
                Color.FromRgb(112, 112, 120)));
            group.Children.Add(new DirectionalLight(
                Color.FromRgb(215, 215, 220),
                new Vector3D(-1.0, -1.4, -2.2)));
            group.Children.Add(new DirectionalLight(
                Color.FromRgb(120, 120, 128),
                new Vector3D(1.6, 1.0, 0.6)));
            // See-through trays first, opaque boxes on top.
            AddModel(group, trayMesh, TrayColour);
            AddModel(group, panelMesh, PanelColour);
            AddModel(group, routeMesh, RouteColour);
            AddModel(group, overMesh, OverColour);
            AddModel(group, pickedMesh, PickedColour);

            Bounds used = frame.HasAny ? frame : trayBounds;
            if (used.HasAny)
            {
                scene.HasBounds = true;
                scene.Low = used.Low;
                scene.High = used.High;
                // Centre the scene on the origin and scale it to about ten
                // units, so one camera distance suits any size of job.
                double extent = Math.Max(Math.Max(
                    used.High.X - used.Low.X, used.High.Y - used.Low.Y),
                    Math.Max(used.High.Z - used.Low.Z, Geometry.Epsilon));
                double scale = 10.0 / extent;
                Transform3DGroup transform = new Transform3DGroup();
                transform.Children.Add(new TranslateTransform3D(
                    -(used.Low.X + used.High.X) / 2.0,
                    -(used.Low.Y + used.High.Y) / 2.0,
                    -(used.Low.Z + used.High.Z) / 2.0));
                transform.Children.Add(new ScaleTransform3D(
                    scale, scale, scale));
                group.Transform = transform;
            }
            scene.Content = group;
            return scene;
        }

        private static Pt[] ItemCorners(EquipmentItem item)
        {
            if (item.HasBounds)
            {
                return Geometry.BoxCorners(item.BoundsLow, item.BoundsHigh);
            }
            if (item.Points.Count == 0)
            {
                return null;
            }
            // No bounding box: draw a small marker at the attachment point.
            double pad = Units.MmToFeet(250.0);
            Pt point = item.Points[0];
            return Geometry.BoxCorners(
                new Pt(point.X - pad, point.Y - pad, point.Z - pad),
                new Pt(point.X + pad, point.Y + pad, point.Z + pad));
        }

        private static void AddModel(
            Model3DGroup group, MeshGeometry3D mesh, Color colour)
        {
            if (mesh.Positions.Count == 0)
            {
                return;
            }
            DiffuseMaterial material = new DiffuseMaterial(
                new SolidColorBrush(colour));
            GeometryModel3D model = new GeometryModel3D(mesh, material);
            model.BackMaterial = material;      // no box can look inside out
            group.Children.Add(model);
        }

        private static void AddBox(MeshGeometry3D mesh, Pt[] corners)
        {
            int baseIndex = mesh.Positions.Count;
            for (int index = 0; index < corners.Length; index++)
            {
                mesh.Positions.Add(new Point3D(
                    corners[index].X, corners[index].Y, corners[index].Z));
            }
            for (int index = 0; index < BoxTriangles.Length; index++)
            {
                mesh.TriangleIndices.Add(baseIndex + BoxTriangles[index]);
            }
        }

        /// <summary>Draw a run of points as a chain of tubes.</summary>
        private static int AddPolyline(
            MeshGeometry3D mesh, IList<Pt> points, double halfWidth,
            Bounds bounds, int limit)
        {
            int added = 0;
            for (int index = 1; index < points.Count; index++)
            {
                Pt from = points[index - 1];
                Pt to = points[index];
                if (from.DistanceTo(to) <= halfWidth)
                {
                    continue;       // a step too short to see
                }
                Pt[] corners = Geometry.TubeCorners(
                    from, to, halfWidth, halfWidth);
                if (corners == null)
                {
                    continue;
                }
                AddBox(mesh, corners);
                bounds.Include(corners);
                added++;
                if (limit > 0 && added >= limit)
                {
                    break;
                }
            }
            return added;
        }

        /// <summary>Running extent of everything drawn.</summary>
        private class Bounds
        {
            private bool _any;
            private double _lowX = double.MaxValue;
            private double _lowY = double.MaxValue;
            private double _lowZ = double.MaxValue;
            private double _highX = double.MinValue;
            private double _highY = double.MinValue;
            private double _highZ = double.MinValue;

            public bool HasAny
            {
                get { return _any; }
            }

            public Pt Low
            {
                get { return new Pt(_lowX, _lowY, _lowZ); }
            }

            public Pt High
            {
                get { return new Pt(_highX, _highY, _highZ); }
            }

            public void Include(Pt[] points)
            {
                for (int index = 0; index < points.Length; index++)
                {
                    Pt point = points[index];
                    _any = true;
                    if (point.X < _lowX) { _lowX = point.X; }
                    if (point.Y < _lowY) { _lowY = point.Y; }
                    if (point.Z < _lowZ) { _lowZ = point.Z; }
                    if (point.X > _highX) { _highX = point.X; }
                    if (point.Y > _highY) { _highY = point.Y; }
                    if (point.Z > _highZ) { _highZ = point.Z; }
                }
            }
        }
    }
}
