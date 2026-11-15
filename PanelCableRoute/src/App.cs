// -----------------------------------------------------------------------------
// The add-in entry point: it builds the ribbon tab and the button that runs
// the command. Revit creates one instance of this class when it starts.
// -----------------------------------------------------------------------------

using System;
using System.Reflection;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Autodesk.Revit.UI;

namespace PanelCableRoute
{
    public class App : IExternalApplication
    {
        public const string TabName = "Electrical Tools";
        public const string PanelName = "Cable routes";

        public Result OnStartup(UIControlledApplication application)
        {
            try
            {
                CreateTab(application, TabName);
                RibbonPanel panel = application.CreateRibbonPanel(
                    TabName, PanelName);

                string assemblyPath = Assembly.GetExecutingAssembly().Location;
                PushButtonData data = new PushButtonData(
                    "PanelCableRoutePro",
                    "Panel cable\nroutes",
                    assemblyPath,
                    "PanelCableRoute.Commands.PanelRouteCommand");
                data.ToolTip = "Panel-to-panel cable tray route lengths for "
                    + "voltage-drop calculations.";
                data.LongDescription =
                    "Measures the route along the modelled cable trays between "
                    + "every electrical panel, and from each panel to the "
                    + "equipment and isolators on its circuits. Shows the "
                    + "routes in 3D before anything is drawn, works out the "
                    + "voltage drop, draws the routes as red detail lines and "
                    + "exports the table to Excel or CSV.\n\n"
                    + "Open the plan view you want the routes drawn in first.";

                PushButton button = panel.AddItem(data) as PushButton;
                if (button != null)
                {
                    ImageSource icon = RouteIcon(32);
                    if (icon != null)
                    {
                        button.LargeImage = icon;
                    }
                    ImageSource small = RouteIcon(16);
                    if (small != null)
                    {
                        button.Image = small;
                    }
                }
                return Result.Succeeded;
            }
            catch (Exception error)
            {
                // A failure here would stop Revit loading the add-in quietly,
                // so say what happened rather than disappearing.
                TaskDialog.Show("Panel Cable Route Pro",
                    "The add-in could not build its ribbon:\n\n"
                    + error.Message);
                return Result.Failed;
            }
        }

        public Result OnShutdown(UIControlledApplication application)
        {
            return Result.Succeeded;
        }

        private static void CreateTab(
            UIControlledApplication application, string name)
        {
            try
            {
                application.CreateRibbonTab(name);
            }
            catch (Exception)
            {
                // Another add-in has already made a tab with this name, which
                // is fine: CreateRibbonPanel will use it.
            }
        }

        /// <summary>
        /// The button icon, drawn in code so the add-in has no image files to
        /// lose. A panel, a tray run and a drop to a second panel.
        /// </summary>
        private static ImageSource RouteIcon(int size)
        {
            try
            {
                DrawingVisual visual = new DrawingVisual();
                using (DrawingContext context = visual.RenderOpen())
                {
                    double scale = size / 32.0;
                    Pen tray = new Pen(
                        new SolidColorBrush(Color.FromRgb(0x94, 0xA3, 0xB8)),
                        3.0 * scale);
                    Pen route = new Pen(
                        new SolidColorBrush(Color.FromRgb(0xDC, 0x26, 0x26)),
                        2.2 * scale);
                    Brush panelBrush = new SolidColorBrush(
                        Color.FromRgb(0x25, 0x63, 0xEB));

                    // transparent background, so the ribbon theme shows through
                    context.DrawRectangle(Brushes.Transparent, null,
                        new Rect(0, 0, size, size));
                    // the tray run across the middle
                    context.DrawLine(tray,
                        new Point(3 * scale, 13 * scale),
                        new Point(29 * scale, 13 * scale));
                    // the route: up from the left panel, along, down to the right
                    context.DrawLine(route,
                        new Point(7 * scale, 25 * scale),
                        new Point(7 * scale, 13 * scale));
                    context.DrawLine(route,
                        new Point(7 * scale, 13 * scale),
                        new Point(25 * scale, 13 * scale));
                    context.DrawLine(route,
                        new Point(25 * scale, 13 * scale),
                        new Point(25 * scale, 25 * scale));
                    // a panel at each end
                    context.DrawRectangle(panelBrush, null, new Rect(
                        3 * scale, 24 * scale, 8 * scale, 6 * scale));
                    context.DrawRectangle(panelBrush, null, new Rect(
                        21 * scale, 24 * scale, 8 * scale, 6 * scale));
                }
                RenderTargetBitmap bitmap = new RenderTargetBitmap(
                    size, size, 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(visual);
                bitmap.Freeze();
                return bitmap;
            }
            catch (Exception)
            {
                return null;        // a button with no icon still works
            }
        }
    }
}
