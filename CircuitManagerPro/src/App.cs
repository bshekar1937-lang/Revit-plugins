using System;
using System.Linq;
using System.Reflection;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Color = System.Windows.Media.Color;
using Point = System.Windows.Point;
using Rect = System.Windows.Rect;

namespace CircuitManagerPro
{
    /// <summary>Revit add-in entry point: creates the ribbon button and the
    /// ExternalEvent the modeless panel uses to talk to the Revit API.</summary>
    public class App : IExternalApplication
    {
        public const string TabName = "Circuit Manager";
        public const string PanelName = "Electrical";

        internal static ApiQueue Queue;
        internal static MainWindow Window;

        public Result OnStartup(UIControlledApplication app)
        {
            Queue = new ApiQueue();
            Queue.Event = ExternalEvent.Create(Queue);

            try { app.CreateRibbonTab(TabName); }
            catch (Exception) { /* tab already exists */ }
            RibbonPanel panel = app.GetRibbonPanels(TabName).FirstOrDefault(p => p.Name == PanelName)
                                ?? app.CreateRibbonPanel(TabName, PanelName);

            var data = new PushButtonData(
                "CircuitManagerPro", "Circuit\nManager Pro",
                Assembly.GetExecutingAssembly().Location, typeof(Command).FullName)
            {
                ToolTip = "Create one circuit per fixture, move / disconnect / delete circuits in bulk, "
                          + "add or remove elements, and check and edit load classifications.",
                LongDescription = "Opens a panel that stays open while you keep working in Revit. "
                                  + "Every action is one Undo step.",
                LargeImage = MakeIcon(32),
                Image = MakeIcon(16),
            };
            panel.AddItem(data);
            return Result.Succeeded;
        }

        public Result OnShutdown(UIControlledApplication app)
        {
            try { Window?.Win.Close(); } catch (Exception) { }
            Queue?.Event?.Dispose();
            return Result.Succeeded;
        }

        /// <summary>Lightning bolt on a blue rounded tile, drawn in code.</summary>
        internal static BitmapSource MakeIcon(int size)
        {
            double s = size;
            var dv = new DrawingVisual();
            using (DrawingContext dc = dv.RenderOpen())
            {
                var bg = new LinearGradientBrush(Color.FromRgb(0x2F, 0x7B, 0xEA), Color.FromRgb(0x0F, 0x24, 0x47), 90);
                double r = s * 0.2;
                dc.DrawRoundedRectangle(bg, null, new Rect(0.5, 0.5, s - 1, s - 1), r, r);

                var bolt = new StreamGeometry();
                using (StreamGeometryContext g = bolt.Open())
                {
                    g.BeginFigure(new Point(0.60 * s, 0.07 * s), true, true);
                    g.LineTo(new Point(0.22 * s, 0.56 * s), true, false);
                    g.LineTo(new Point(0.47 * s, 0.56 * s), true, false);
                    g.LineTo(new Point(0.37 * s, 0.93 * s), true, false);
                    g.LineTo(new Point(0.79 * s, 0.41 * s), true, false);
                    g.LineTo(new Point(0.54 * s, 0.41 * s), true, false);
                }
                bolt.Freeze();
                var gold = new LinearGradientBrush(Color.FromRgb(0xFF, 0xE0, 0x82), Color.FromRgb(0xFF, 0x8F, 0x00), 90);
                var edge = new Pen(new SolidColorBrush(Color.FromRgb(0x5D, 0x34, 0x00)), Math.Max(0.6, s / 32.0));
                dc.DrawGeometry(gold, edge, bolt);
            }
            var bmp = new RenderTargetBitmap(size, size, 96, 96, PixelFormats.Pbgra32);
            bmp.Render(dv);
            bmp.Freeze();
            return bmp;
        }
    }

    /// <summary>Ribbon button: opens the panel, or brings it to the front.</summary>
    [Transaction(TransactionMode.Manual)]
    public class Command : IExternalCommand
    {
        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            UIDocument uidoc = data.Application.ActiveUIDocument;
            if (uidoc == null || uidoc.Document.IsFamilyDocument)
            {
                TaskDialog.Show("Circuit Manager Pro",
                    "Open a project first - Circuit Manager Pro does not work in the Family Editor.");
                return Result.Cancelled;
            }

            if (App.Window != null)
            {
                if (App.Window.IsFor(uidoc.Document))
                {
                    App.Window.BringToFront();
                    return Result.Succeeded;
                }
                App.Window.Win.Close();      // it belongs to another project
                App.Window = null;
            }

            try
            {
                App.Window = new MainWindow(data.Application, App.Queue);
                App.Window.Win.Show();
                return Result.Succeeded;
            }
            catch (Exception ex)
            {
                App.Window = null;
                message = ex.Message;
                return Result.Failed;
            }
        }
    }
}
