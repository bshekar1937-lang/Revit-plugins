using System;
using System.Globalization;
using System.Reflection;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Autodesk.Revit.UI;
using WpfColor = System.Windows.Media.Color;

namespace MyRevitCommands
{
    /// <summary>
    /// Creates your own ribbon tab (like pyRevit / DiRoots) when Revit starts.
    /// Registered in the .addin file as Type="Application".
    /// </summary>
    public class App : IExternalApplication
    {
        // Change these two names to anything you like.
        private const string TabName = "My Tools";
        private const string PanelName = "Electrical";

        public Result OnStartup(UIControlledApplication application)
        {
            try
            {
                try { application.CreateRibbonTab(TabName); }
                catch (Autodesk.Revit.Exceptions.ArgumentException) { /* tab already exists */ }

                RibbonPanel panel = application.CreateRibbonPanel(TabName, PanelName);
                string dll = Assembly.GetExecutingAssembly().Location;

                var panelDistance = new PushButtonData(
                    "PanelDistance",                                  // internal name (unique)
                    "Panel\nDistance",                                // text under the button
                    dll,
                    typeof(PanelDistanceCommand).FullName)            // MyRevitCommands.PanelDistanceCommand
                {
                    ToolTip = "Measure cable routes between panels and equipment along cable trays, with voltage drop.",
                    LongDescription = "Pick panels, measure tray routes, check voltage drop, draw red detail lines and export CSV.",
                    LargeImage = MakeIcon(32, "PD", "#2563EB"),
                    Image = MakeIcon(16, "P", "#2563EB")
                };
                panel.AddItem(panelDistance);

                var lightPlacement = new PushButtonData(
                    "LightPlacement2D",
                    "Light\nPlacement",
                    dll,
                    typeof(LightPlacementCommand).FullName)
                {
                    ToolTip = "Place your 2D family at every lighting fixture of a linked model (matches or creates types).",
                    LargeImage = MakeIcon(32, "LP", "#D97706"),
                    Image = MakeIcon(16, "L", "#D97706")
                };
                panel.AddItem(lightPlacement);

                // To add another tool later, copy the block above:
                // var other = new PushButtonData("OtherTool", "Other\nTool", dll, typeof(OtherCommand).FullName) { ... };
                // panel.AddItem(other);

                return Result.Succeeded;
            }
            catch (Exception exception)
            {
                TaskDialog.Show(TabName, "Could not create the ribbon:\n" + exception.Message);
                return Result.Failed;
            }
        }

        public Result OnShutdown(UIControlledApplication application)
        {
            return Result.Succeeded;
        }

        /// <summary>
        /// Draws a simple coloured icon in code, so no image files or resource
        /// settings are needed. Replace with your own PNGs later if you want.
        /// </summary>
        private static BitmapSource MakeIcon(int size, string text, string hex)
        {
            var visual = new DrawingVisual();
            using (DrawingContext context = visual.RenderOpen())
            {
                var fill = new SolidColorBrush((WpfColor)ColorConverter.ConvertFromString(hex));
                context.DrawRoundedRectangle(fill, null, new Rect(0, 0, size, size), size / 5.0, size / 5.0);
                var label = new FormattedText(
                    text,
                    CultureInfo.InvariantCulture,
                    FlowDirection.LeftToRight,
                    new Typeface(new FontFamily("Segoe UI"), FontStyles.Normal, FontWeights.Bold, FontStretches.Normal),
                    size * (text.Length > 1 ? 0.42 : 0.62),
                    Brushes.White,
                    1.0);
                context.DrawText(label, new Point((size - label.Width) / 2, (size - label.Height) / 2));
            }
            var bitmap = new RenderTargetBitmap(size, size, 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(visual);
            bitmap.Freeze();
            return bitmap;
        }
    }
}
